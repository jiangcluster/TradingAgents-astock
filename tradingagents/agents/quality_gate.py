from typing import Annotated

import logging
import re

logger = logging.getLogger(__name__)

# markdown 表格分隔行：整行只由 `| - : 空白` 组成，且**至少含一个 `|` 与一个 `-`**
# （覆盖 `| --- | --- |`、`|:--|--:|`、单列表格等常见写法）。
# 0.5.38：此前判据是 `"|" in report and "---" in report` —— 报告里只要同时出现一个竖线
# 与一行 `---`（分节线，甚至正文里的破折号串）就会被当成"有汇总表格"。
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$", re.M)

REPORT_FIELDS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "policy": "policy_report",
    "hot_money": "hot_money_report",
    "lockup": "lockup_report",
}

ANALYST_NAMES = {
    "market": "技术分析师",
    "social": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
    "policy": "政策分析师",
    "hot_money": "游资追踪师",
    "lockup": "解禁监控师",
}

MIN_REPORT_LENGTH = 200

# 取数失败的**实际文案**（数据层）+ 模型自己承认取不到的措辞。
# ⚠️ 这份词表必须与数据层实际返回值对齐：a_stock 失败时返回的是
# "K线数据获取失败：…" / "Error fetching hot stocks for …" / "…查询失败"，而原先
# 只列了"无法获取"/"unable to fetch" —— 真正失败的报告反而认不出来（长报告里夹带
# 一行失败信息时尤其明显，长度兜底管不到）。
# 0.5.28（批 J / G14）：补 `Error retrieving …`（a_stock 的 fundamentals / balance sheet /
# cash flow / income statement / insider-shareholder / profit forecast 六处**都是**这个动词）
# 与 `Error calculating …`（技术指标），以及 `No … data` 一族——此前词表只有 `Error fetching`，
# 而"取数失败"文案里占多数的是 `Error retrieving`，等于门控看不出来。
# 防漂移：`tests/test_quality_gate.py::test_failure_markers_cover_data_layer_failure_strings`
# 会从 `a_stock.py` 源码提取失败文案并断言词表覆盖（新增失败文案忘记同步即失败）。
#
# 0.5.36（批次 C）：补 9 条**实测提取**出的缺口文案——此前守卫只提取
# `return f"Error <verb>` / `return f"No <x> data` 两种**单行**形态，而跨行拼接的
# `return (\n f"…"`（`No hot stocks data…` / `No data found…` / `Baidu PAE error…`）
# 与中文前缀的 `同花顺 API error:` **全部漏检** → 守卫形同空转。
# 现判据改为**与门控实现完全一致**（"至少有一个 marker 是该文案的子串"，
# 门控的实际判据就是 `m in report`），新增文案未同步词表即测试失败。
FAILURE_MARKERS = [
    "无法获取",
    "获取失败",
    "查询失败",
    "工具调用失败",
    "I cannot retrieve",
    "I don't have access",
    "unable to fetch",
    "Error fetching",
    "Error retrieving",
    "Error calculating",
    "No fundamentals data",
    "No balance sheet data",
    "No cash flow data",
    "No income statement data",
    "No insider/shareholder data",
    "No concept/block data",
    # —— 0.5.36 补：实测提取出的 9 条缺口（见上方注释）——
    "PAE error",                    # 百度股市通失败（Baidu PAE error: ResultCode=…）
    "API error",                    # 同花顺失败（同花顺 API error: …）
    "No analyst coverage",          # 一致预期无覆盖（No analyst coverage found for …）
    "No data found",                # K 线兜底为空（No data found for A-stock …）
    "No global news available",     # 全球新闻源覆盖不足
    "No global news found",         # 全球新闻为空
    "No hot stocks data",           # 游资/热门股为空
    "No news found for",            # 个股新闻为空
    "未提供分析日期",                # 缺 curr_date → 时点截断失效（_missing_curr_date_notice）
]


def _hard_check_report(analyst_type: str, report: str) -> tuple:
    """Run hard checks on a single report. Returns (grade, detail)."""
    if not report or not report.strip():
        return ("F", "报告为空")

    length = len(report.strip())
    if length < MIN_REPORT_LENGTH:
        return ("D", f"报告过短 ({length} chars < {MIN_REPORT_LENGTH})")

    failure_count = sum(1 for m in FAILURE_MARKERS if m in report)
    stripped = report
    for m in FAILURE_MARKERS:
        stripped = stripped.replace(m, "")
    if failure_count > 0 and len(stripped.strip()) < MIN_REPORT_LENGTH:
        return ("D", f"报告主要由失败信息构成 ({failure_count} 处)")

    has_table = bool(_TABLE_SEP_RE.search(report))
    missing_count = report.count("[数据缺失")

    issues = []
    if not has_table:
        issues.append("缺少汇总表格")
    if missing_count > 0:
        issues.append(f"{missing_count} 处数据缺失")
    # 0.5.37：取数失败文案**同样计入降级判据**（此前只服务于上面的 D 判据）——
    # "长报告里夹带一处取数失败"此前照样能判 A（称"完整"），与事实不符。
    # 阈值与 missing_count 对齐（≥3 → C，>0 → B）；**不影响** LLM 复审的跳过判据：
    # `_should_skip_review` 只统计 D/F（见 `quality_gate_node` 的 fail_count），B/C 不计入。
    if failure_count > 0:
        issues.append(f"{failure_count} 处取数失败文案")

    if missing_count >= 3 or failure_count >= 3:
        return ("C", "；".join(issues))
    if not has_table or missing_count > 0 or failure_count > 0:
        return ("B", "；".join(issues) if issues else "基本合格")

    return ("A", f"完整 ({length} chars)")


def _active_analysts(state) -> list:
    """本次实际进图的分析师键（按 REPORT_FIELDS 顺序）。

    未选中者不进图、报告必为空——若照旧一律判 F，选 1-3 个分析师时会凭空凑出 ≥4 个 F，
    反而把 LLM 复审整段跳过。故门控**只对已运行的分析师判级**。

    状态里没有 `selected_analysts`（旧 checkpoint / 手工构造 state）时：0.5.38 起退回
    "**报告非空**的分析师"——原实现退回全部 7 项，会把"根本没跑"的分析师当成"跑了但报告
    为空"判 F，凭空凑够 fail_count 触发"跳过 LLM 复审"（本该逐份复核的场景）。留痕照旧。
    """
    selected = state.get("selected_analysts")
    if not selected:
        logger.warning(
            "quality gate: state has no `selected_analysts`; grading only analysts "
            "with a non-empty report. Analysts that never ran are NOT counted as "
            "failures (old checkpoint / hand-built state?).",
        )
        return [a for a in REPORT_FIELDS
                if str(state.get(REPORT_FIELDS[a]) or "").strip()]
    return [a for a in REPORT_FIELDS if a in set(selected)]


def _should_skip_review(fail_count: int, graded: int) -> bool:
    """超过半数**已运行**报告未通过硬检查时，跳过 LLM 复审。

    原判据是硬编码的 ``fail_count >= 4``——它只在"7 个分析师全跑"时等价于"过半"。
    分析师集合可变（1-7 个）时这个分母是错的：选 1 个、那 1 个又为空，按 ≥4 就不会
    跳过（复审一份空报告没有意义），而退回"全部 7 项"的兜底又会凭空凑够 4 个 F。
    改成按比例判断后，两种情形都归到同一语义上。
    """
    return graded > 0 and fail_count * 2 > graded


def _build_review_prompt(
    reports: dict, trade_date: str, ticker: str, analysts: list = None
) -> str:
    """Build the LLM review prompt."""
    analysts = analysts if analysts is not None else list(REPORT_FIELDS)
    report_sections = []
    for analyst_type in analysts:
        field = REPORT_FIELDS[analyst_type]
        name = ANALYST_NAMES[analyst_type]
        content = reports.get(field, "（未运行）")
        if not content:
            content = "（报告为空）"
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated for review)"
        report_sections.append(f"### {name} ({analyst_type})\n{content}")

    all_reports = "\n\n".join(report_sections)

    rows = "\n".join(
        f"| {ANALYST_NAMES[a]} | A/B/C/D/F | 是否匹配交易日 | 列出缺失的必采项 | 简要说明 |"
        for a in analysts
    )
    return f"""你是数据质量审核员。以下是本次实际运行的 {len(analysts)} 位分析师对 {ticker} 在 {trade_date} 的研究报告。请逐一审核。

{all_reports}

---

请按以下格式输出审核结果（不要输出其他内容）：

## 数据质量审核报告

**标的**: {ticker} | **日期**: {trade_date}

| 分析师 | 评级 | 数据时效 | 缺失项 | 备注 |
|--------|------|----------|--------|------|
{rows}

**整体评级**: A/B/C/D/F
**数据可信度**: 高/中/低
**建议**: （如有数据缺失，逐项列出；并明确说明：缺失项是**不确定性**，**不得**作为任何方向的证据
——既不能当作「没有风险」，也不能据此推出看空结论）

评级标准：
- A: 必采清单全部覆盖，数据时效匹配，有汇总表格
- B: 缺少 1-2 项非关键数据，整体可用
- C: 缺少 3+ 项或有数据时效问题，需谨慎使用
- D: 大量缺失或主要为失败信息，可信度低
- F: 报告为空或完全无效
"""


def create_quality_gate(llm):
    """Factory for the data quality gate node.

    Sits between the last analyst Msg Clear and Bull Researcher.
    Layer 1: hard checks (code). Layer 2: LLM review (one call).
    Writes data_quality_summary to state for downstream consumers.
    """

    def quality_gate_node(state) -> dict:
        trade_date = state["trade_date"]
        ticker = state["company_of_interest"]

        analysts = _active_analysts(state)

        reports = {}
        for analyst_type, field in REPORT_FIELDS.items():
            reports[field] = state.get(field, "")

        hard_results = {}
        for analyst_type in analysts:
            field = REPORT_FIELDS[analyst_type]
            grade, detail = _hard_check_report(analyst_type, reports[field])
            hard_results[analyst_type] = (grade, detail)

        hard_summary_lines = []
        for analyst_type, (grade, detail) in hard_results.items():
            name = ANALYST_NAMES[analyst_type]
            hard_summary_lines.append(f"- {name}: [{grade}] {detail}")
        skipped = [a for a in REPORT_FIELDS if a not in set(analysts)]
        for analyst_type in skipped:
            name = ANALYST_NAMES[analyst_type]
            hard_summary_lines.append(f"- {name}: [—] 未运行（本次未选中，不计入质量评级）")
        hard_summary = "\n".join(hard_summary_lines)

        fail_count = sum(
            1 for _, (g, _) in hard_results.items() if g in ("F", "D")
        )

        llm_review = ""
        if not hard_results:
            # 0.5.38：`_active_analysts` 不再把"未运行"当 F 之后，"全部未运行"会走到这里。
            # 对 0 份报告发复审请求没有意义；但也不能静默留空 —— 必须与"数据没问题"区分开。
            llm_review = (
                "（**本次无已运行的分析师报告**，无可复审内容。这**不代表**数据没有问题："
                "下游应按「无证据」处理并主动降权，且不得据「缺数据」推出方向性结论。）"
            )
        elif not _should_skip_review(fail_count, len(hard_results)):
            try:
                review_prompt = _build_review_prompt(reports, trade_date, ticker, analysts)
                response = llm.invoke(review_prompt)
                llm_review = response.content
            except Exception as e:
                llm_review = f"（LLM 复审失败: {type(e).__name__}: {e}）"
        else:
            # 显式说明"复审不可用"而不是静默留空：下游必须知道这不是"数据没问题"
            bad = "、".join(
                ANALYST_NAMES[a] for a, (g, _) in hard_results.items() if g in ("F", "D")
            )
            llm_review = (
                f"（**门控复审不可用**：{fail_count}/{len(hard_results)} 份已运行报告未通过硬检查"
                f"（{bad}），已跳过 LLM 复审。上述报告的可信度未经复核，"
                f"下游应主动降权使用，不要把「缺数据」当作「没有风险」；"
                f"同样**不得**据「缺数据」推出看空结论——缺失是**不确定性**，"
                f"不是方向性证据，须按「哪一方仍有可用证据」来权衡。）"
            )

        scope_line = (
            f"**审核范围**: 本次运行 {len(analysts)} 位分析师"
            + (f"（另 {len(skipped)} 位未选中，未计入评级）" if skipped else "")
        )
        # 工具轮次上限收尾（0.6.7）：把「被护栏截断」与「数据源缺失」在摘要里显式区分开。
        # 此前该事件只写 logger，而 headless 生产丢弃 stderr ⇒ 交付链路上零留痕，
        # 下游（深研）只能看到"某维为空/缺失"，归因被带到数据源方向（实测输出"未归类"）。
        truncations = [a for a in (state.get("analyst_truncations") or []) if a]
        trunc_note = ""
        if truncations:
            names = "、".join(dict.fromkeys(ANALYST_NAMES.get(a, a) for a in truncations))
            trunc_note = (
                f"\n> **[!] 工具轮次上限收尾**：{names} 的取数工具调用达到轮次上限，"
                f"该维报告由护栏**收尾**而成（**非数据源缺失**）。"
                f"以上结论基于截断前已取得的数据，未取到的必采项已在报告中如实标注；"
                f"归因请按「被护栏截断」处理，不要记成数据源不可用。\n"
            )
        summary = (
            f"## 数据质量门控结果\n\n"
            f"**标的**: {ticker} | **交易日**: {trade_date}\n"
            f"{scope_line}\n"
            f"{trunc_note}"
            f"\n### 硬检查结果\n{hard_summary}\n\n"
            f"### LLM 复审\n"
            f"{llm_review if llm_review else '（未执行）'}\n"
        )

        return {"data_quality_summary": summary}

    return quality_gate_node
