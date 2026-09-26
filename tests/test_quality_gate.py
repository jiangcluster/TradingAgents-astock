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


def test_failure_markers_cover_data_layer_failure_strings():
    """词表必须覆盖数据层**实际**的失败文案（G14 / 0.5.28）。

    此前词表只有 `Error fetching`，而 a_stock 的六处财报类失败返回的是
    `Error retrieving …` → 门控认不出取数失败，可能把失败当合格数据给 A/B 级。
    本用例直接从数据层源码提取两族文案并断言词表覆盖：**新增失败文案而忘了同步词表即失败**。
    """
    src = (_ROOT / "tradingagents" / "dataflows" / "a_stock.py").read_text(encoding="utf-8")
    # 只取 `return f"…"` 形态：门控扫的是**进报告的文本**；`raise ValueError("No OHLCV data…")`
    # 这类内部异常消息由调用方转成"K线数据获取失败：…"（已由"获取失败"覆盖），不属词表范围。
    verbs = sorted(set(re.findall(r'return f"Error (\w+)', src)))
    assert verbs, "提取不到 `Error <verb>` 文案（守卫空转，检查数据层文案格式是否变化）"
    markers = "\n".join(qg.FAILURE_MARKERS)
    missing = [v for v in verbs if f"Error {v}" not in markers]
    assert not missing, f"数据层失败文案未进 FAILURE_MARKERS：{[f'Error {v}' for v in missing]}"

    no_data = sorted(set(re.findall(r'return f"No ([\w/\-]+) data', src)))
    assert no_data, "提取不到 `No <x> data` 文案（守卫空转）"
    missing_no = [n for n in no_data if f"No {n} data" not in markers]
    assert not missing_no, f"数据层 No…data 文案未进 FAILURE_MARKERS：{[f'No {n} data' for n in missing_no]}"


FULL_REPORT = (
    "## 分析\n" + "正文内容。" * 100 + "\n\n"
    "| 指标 | 值 |\n| --- | --- |\n| PE | 20 |\n"
)


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


# ---------------------------------------------------------------------------
# 审核范围：只算进图的分析师
# ---------------------------------------------------------------------------


def test_active_analysts_defaults_to_all_when_field_absent():
    """旧调用方/直接构造 state 时退回全部 7 项，行为不变。"""
    assert qg._active_analysts({"trade_date": "2026-09-19"}) == list(qg.REPORT_FIELDS)


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
