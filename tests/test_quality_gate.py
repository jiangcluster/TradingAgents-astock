"""数据质量门控（quality_gate）单测。

门控夹在"分析师 → 多空辩论"之间，结论以 data_quality_summary 流向研究经理、
交易员、风控与组合经理。三件事此前没有任何测试覆盖，但都直接影响结论：

1. 只对**本次进图**的分析师判级——未选中者报告必为空，一律判 F 会在选 1-3 个
   分析师时凭空凑出 ≥4 个 F，把 LLM 复审整段跳过；
2. 复审不可用时必须显式说明，不能静默留空（"没复审"与"数据没问题"长得一样）；
3. 复审提示词的行数与实际审核范围一致，否则模型会为空分析师编评级。
"""

import pathlib
import re

import pytest

from tradingagents.agents import quality_gate as qg

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# 数据层失败文案的**语义对齐**守卫（0.5.36 重写）：
# 判据与门控实现完全一致 —— 门控扫的是 `m in report`（子串命中），故守卫也断言
# "每条失败文案至少被一个 marker 作为**子串**命中"。
# 此前用两条窄正则提取（只认单行 `return f"Error <v>` / `return f"No <x> data`），
# 跨行拼接（`return (\n f"…"`）与中文前缀的文案**全部漏检** —— 实测漏 9 条
# （`No hot stocks data…` / `No data found…` / `Baidu PAE error…` / `同花顺 API error:` …），
# 等于词表补齐了也无人守（0.5.28 立的守卫形同空转）。
_RETURN_LITERAL_RE = re.compile(r'return\s*\(?\s*f?"((?:[^"\\]|\\.)*)"')
_FAILURE_HINTS = ("error", "Error", "失败", "not found", "No ", "无法",
                  "unavailable", "no data", "未提供")


def _data_layer_failure_literals():
    """从数据层源码提取所有"失败语气"的 `return` 字符串字面量（插值归一为 `{}`）。"""
    src = (_ROOT / "tradingagents" / "dataflows" / "a_stock.py").read_text(encoding="utf-8")
    out = set()
    for lit in _RETURN_LITERAL_RE.findall(src):
        flat = re.sub(r"\{[^}]*\}", "{}", lit)
        if any(h in flat for h in _FAILURE_HINTS):
            out.add(flat)
    return out


def test_guard_extractor_is_not_vacuous():
    """守卫自身不得空转：必须连**跨行拼接**与**中文前缀**的文案都能提取到。"""
    lits = _data_layer_failure_literals()
    assert len(lits) >= 20, f"提取到的失败文案过少，守卫可能已失效：{sorted(lits)}"
    joined = "\n".join(lits)
    assert "No hot stocks data" in joined, "跨行拼接的失败文案未被提取（守卫空转）"
    assert "API error" in joined, "中文前缀的失败文案未被提取（守卫空转）"


def test_failure_markers_cover_data_layer_failure_strings():
    """词表必须覆盖数据层**实际**的失败文案（0.5.28 立、0.5.36 改判据）。

    未覆盖的后果：门控把"取数失败"当合格数据给 A/B 级 —— 长度兜底管不到"长报告里
    夹带一行失败信息"的场景（该场景下 failure 计数只在 D 判据里起作用）。
    判据 = `存在某个 marker 是该文案的子串`，与门控的 `m in report` 完全一致。
    """
    lits = _data_layer_failure_literals()
    assert lits, "提取不到数据层失败文案（守卫空转，检查提取正则与文案形态）"
    markers = qg.FAILURE_MARKERS
    missing = sorted(lit for lit in lits if not any(m in lit for m in markers))
    assert not missing, (
        "以下数据层失败文案**没有任何 FAILURE_MARKERS 子串命中**（门控会把它们当合格数据）：\n  "
        + "\n  ".join(missing)
        + "\n修复：把对应子串加入 `quality_gate.FAILURE_MARKERS`。"
    )


@pytest.mark.parametrize("failure_text", [
    "No hot stocks data for 2026-09-24 (may be non-trading day or data not yet available)",
    "同花顺 API error: timeout",
    "No analyst coverage found for A-stock '600519'",
    "Baidu PAE error: ResultCode=1001 参数错误",
    "No data found for A-stock '600519' between 2026-01-01 and 2026-09-24",
    "No news found for A-stock '600519'",
    "No global news found for 2026-09-24",
    "⚠️ 未提供分析日期（curr_date）：无法剔除分析日之后才披露的报告期，",
])
def test_known_failure_texts_are_recognized(failure_text):
    """0.5.36 补齐的 9 条缺口逐条回归：门控必须把它们认成失败。"""
    report = "## 分析\n" + "正文内容。" * 200 + "\n\n" + failure_text + "\n"
    assert any(m in report for m in qg.FAILURE_MARKERS), \
        f"门控认不出该失败文案（会当合格数据处理）：{failure_text!r}"


FULL_REPORT = (
    "## 分析\n" + "正文内容。" * 100 + "\n\n"
    "| 指标 | 值 |\n| --- | --- |\n| PE | 20 |\n"
)


# ---------------------------------------------------------------------------
# 0.5.37：取数失败文案纳入 A/B/C 降级判据（此前只服务 D 判据）
# ---------------------------------------------------------------------------
_LONG_BODY = "正文内容。" * 200 + "\n\n| 指标 | 值 |\n| --- | --- |\n| PE | 20 |\n"


def test_long_report_with_one_failure_falls_to_b():
    """长报告里夹带一处取数失败 → 至少 B（此前照样判 A，称"完整"与事实不符）。"""
    grade, detail = qg._hard_check_report(
        "news", _LONG_BODY + "\nNo hot stocks data for 2026-09-24\n")
    assert grade == "B", detail
    assert "取数失败文案" in detail


def test_long_report_with_three_failures_falls_to_c():
    text = _LONG_BODY + "\n" + "\n".join([
        "No hot stocks data for 2026-09-24",
        "同花顺 API error: timeout",
        "No news found for A-stock '600519'",
    ])
    grade, detail = qg._hard_check_report("news", text)
    assert grade == "C", detail


def test_clean_long_report_still_gets_a():
    """防过矫：无失败文案的完整报告仍是 A。"""
    grade, _ = qg._hard_check_report("news", _LONG_BODY)
    assert grade == "A"


def test_failure_downgrade_does_not_enter_hard_fail_count():
    """关键边界：B/C **不得**计入 `fail_count`（否则会误触发"跳过 LLM 复审"）。"""
    grade, _ = qg._hard_check_report(
        "news", _LONG_BODY + "\nNo hot stocks data for 2026-09-24\n")
    assert grade not in ("F", "D")


class FakeLLM:
    def __init__(self, content="## 数据质量审核报告\n**整体评级**: A", error=None):
        self.content = content
        self.error = error
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error

        class _Resp:
            pass

        resp = _Resp()
        resp.content = self.content
        return resp


def _state(**overrides):
    base = {
        "trade_date": "2026-09-19",
        "company_of_interest": "600519",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 硬检查
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report,expected",
    [
        ("", "F"),
        ("   ", "F"),
        ("太短了", "D"),
        ("无法获取数据 " * 40, "D"),
        ("正文。" * 200, "B"),                      # 够长但缺汇总表格
        (FULL_REPORT, "A"),
    ],
)
def test_hard_check_grades(report, expected):
    grade, _ = qg._hard_check_report("market", report)
    assert grade == expected


def test_hard_check_flags_three_or_more_missing_items_as_c():
    report = FULL_REPORT + "\n[数据缺失: PE] [数据缺失: PB] [数据缺失: ROE]"

    grade, detail = qg._hard_check_report("fundamentals", report)

    assert grade == "C"
    assert "3 处数据缺失" in detail


def test_hard_check_long_report_with_one_missing_item_is_b():
    report = FULL_REPORT + "\n[数据缺失: PE]"

    assert qg._hard_check_report("fundamentals", report)[0] == "B"


@pytest.mark.parametrize("report,expect_table", [
    (FULL_REPORT, True),
    ("正文。" * 100 + "\n\n| 指标 | 值 |\n|:--|--:|\n| PE | 20 |\n", True),
    ("正文。" * 100 + "\n\n|---|\n", True),                        # 单列表格
    ("正文。" * 100 + "\n\n---\n\n备注 | 说明\n", False),           # 分节线 + 正文竖线 ≠ 表格
    ("正文。" * 100, False),
])
def test_table_detection_requires_a_real_separator_row(report, expect_table):
    """0.5.38：表格判据由「含 `|` 且含 `---`」收紧为「存在合法 markdown 分隔行」。"""
    assert bool(qg._TABLE_SEP_RE.search(report)) is expect_table


def test_old_lenient_table_rule_misjudged_prose():
    """旧判据（`"|" in report and "---" in report`）会把"分节线 + 正文竖线"当表格。"""
    text = "正文。" * 100 + "\n\n---\n\n补充说明 | 备注\n"

    assert "|" in text and "---" in text          # 旧判据在此判"有表"（与事实不符）
    assert not qg._TABLE_SEP_RE.search(text)      # 新判据正确判"无表"
    assert qg._hard_check_report("market", text)[0] == "B"


# ---------------------------------------------------------------------------
# 审核范围：只算进图的分析师
# ---------------------------------------------------------------------------


def test_active_analysts_without_selection_field_grades_only_non_empty():
    """0.5.38：缺 `selected_analysts` 时只对**报告非空**者判级（不再退回全部 7 项）。

    原实现把"根本没跑"的分析师当成"跑了但报告为空"判 F —— 凭空凑够 fail_count 会触发
    "跳过 LLM 复审"，恰恰是本该逐份复核的场景。空报告不算证据，也不该算失败。
    """
    assert qg._active_analysts(
        {"trade_date": "2026-09-19", "market_report": FULL_REPORT}) == ["market"]
    assert qg._active_analysts({"trade_date": "2026-09-19"}) == []
    assert qg._active_analysts(
        {"trade_date": "2026-09-19", "market_report": "   "}) == []      # 空白串不算已运行


def test_active_analysts_keeps_only_selected_in_canonical_order():
    selected = ["lockup", "market", "hot_money"]

    assert qg._active_analysts({"selected_analysts": selected}) == [
        "market", "hot_money", "lockup"
    ]


def test_active_analysts_ignores_unknown_keys():
    assert qg._active_analysts({"selected_analysts": ["market", "nope"]}) == ["market"]


# ---------------------------------------------------------------------------
# 门控节点
# ---------------------------------------------------------------------------


def test_gate_grades_only_selected_analysts():
    """未选中的分析师报告必为空，判 F 只会污染 fail_count。"""
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)

    out = node(
        _state(
            selected_analysts=["market"],
            market_report=FULL_REPORT,
        )
    )["data_quality_summary"]

    assert "技术分析师: [A]" in out
    assert "基本面分析师: [—] 未运行（本次未选中，不计入质量评级）" in out
    assert "本次运行 1 位分析师（另 6 位未选中，未计入评级）" in out
    assert llm.prompts, "门控应当执行 LLM 复审"


def test_gate_with_no_reports_says_so_and_skips_review():
    """0.5.38：全部未运行（或报告全空）→ 显式说明，且**不**发复审请求。

    此前这种 state 会被判 7 个 F：既凭空拉低质量评级、又可能触发"跳过复审"，
    而真相是"没有任何证据可审" —— 必须与「数据没问题」区分开（下游据此降权）。
    """
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)

    out = node(_state())["data_quality_summary"]

    assert "本次无已运行的分析师报告" in out
    assert "不代表" in out
    assert not llm.prompts, "没有可审报告时不该调用 LLM 复审"
    assert "[F]" not in out, "不得把未运行的分析师判 F"


def test_gate_reviews_when_minority_of_selected_reports_fail():
    """3 个选中、其中 1 个为空 → 未过半，仍要复审。

    修前按 7 项判级：未选中者一律 F，凭空凑够 fail_count≥4 → 复审被跳过。
    """
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)

    out = node(
        _state(
            selected_analysts=["market", "social", "news"],
            market_report=FULL_REPORT,
            sentiment_report=FULL_REPORT,
        )
    )["data_quality_summary"]
    assert llm.prompts, "只有 1/3 未通过，不该跳过复审"
    assert "门控复审不可用" not in out


def test_gate_skips_review_when_majority_of_selected_reports_fail():
    """3 个选中、全部为空 → 过半未通过，跳过复审并说明比例。

    「跳过」的判据从硬编码的 `>= 4` 改成「超过半数已运行报告未通过」：原阈值只在
    7 个分析师全跑时才等价于过半，分析师集合可变时分母是错的。
    """
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)

    out = node(_state(selected_analysts=["market", "social", "news"]))["data_quality_summary"]

    assert not llm.prompts, "过半报告未通过硬检查时不应再调用 LLM"
    assert "门控复审不可用" in out
    assert "3/3 份已运行报告未通过硬检查" in out


def test_gate_skips_review_on_four_of_seven():
    """7 个全跑、4 个未通过 → 跳过的边界与旧行为一致（4 是 7 的过半）。"""
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)
    state = _state(selected_analysts=list(qg.REPORT_FIELDS))
    state["market_report"] = FULL_REPORT
    state["sentiment_report"] = FULL_REPORT
    state["news_report"] = FULL_REPORT

    out = node(state)["data_quality_summary"]

    assert not llm.prompts
    assert "4/7 份已运行报告未通过硬检查" in out
    assert "不要把「缺数据」当作「没有风险」" in out


def test_gate_reports_llm_failure_without_raising():
    llm = FakeLLM(error=RuntimeError("gateway timeout"))
    node = qg.create_quality_gate(llm)

    out = node(_state(selected_analysts=["market"], market_report=FULL_REPORT))[
        "data_quality_summary"
    ]

    assert "LLM 复审失败" in out
    assert "RuntimeError" in out


def test_gate_returns_only_the_summary_key():
    node = qg.create_quality_gate(FakeLLM())

    result = node(_state(selected_analysts=["market"], market_report=FULL_REPORT))

    assert set(result) == {"data_quality_summary"}
    assert result["data_quality_summary"].startswith("## 数据质量门控结果")


def test_review_prompt_covers_exactly_the_graded_analysts():
    """提示词必须与审核范围一致：多列会让模型给未运行的分析师编评级。"""
    reports = {field: "（未运行）" for field in qg.REPORT_FIELDS.values()}

    prompt = qg._build_review_prompt(reports, "2026-09-19", "600519", ["market", "lockup"])

    assert "本次实际运行的 2 位分析师" in prompt
    assert "技术分析师" in prompt and "解禁监控师" in prompt
    assert "基本面分析师" not in prompt
    assert prompt.count("| A/B/C/D/F |") == 2


def test_review_prompt_defaults_to_all_analysts():
    reports = {field: "（未运行）" for field in qg.REPORT_FIELDS.values()}

    prompt = qg._build_review_prompt(reports, "2026-09-19", "600519")

    assert "本次实际运行的 7 位分析师" in prompt


# ---------------------------------------------------------------------------
# 0.5.31：数据缺失的措辞必须**方向中性**（缺失 ≠ 看空）
# ---------------------------------------------------------------------------
def test_review_prompt_states_missing_data_is_direction_neutral():
    """复审提示语不得只提醒"谨慎使用"——那等于把"缺数据"读成看空证据。

    实测背景（2026-09-21~09-24）：资金流/龙虎榜/北向大面积缺失，而**多方框架**
    （北向流入、游资接力、量价资金）恰好依赖这些数据；此时任何"单向提示"都会
    让天平倒向空方（该窗口 24 票终裁无一条 Buy/Overweight）。
    """
    reports = {field: "（未运行）" for field in qg.REPORT_FIELDS.values()}

    prompt = qg._build_review_prompt(reports, "2026-09-19", "600519")

    assert "不得**作为任何方向的证据" in prompt
    assert "也不能据此推出看空结论" in prompt


def test_skip_review_note_states_missing_data_is_direction_neutral():
    llm = FakeLLM()
    node = qg.create_quality_gate(llm)

    out = node(_state(selected_analysts=["market", "social", "news"]))["data_quality_summary"]

    assert not llm.prompts, "过半报告未通过硬检查时不应再调用 LLM"
    assert "不得**据「缺数据」推出看空结论" in out
    assert "不要把「缺数据」当作「没有风险」" in out   # 两个方向都要堵
