"""工具轮次上限的**收尾兜底**（0.6.7）单测。

背景：graph 层护栏在达上限时直接收口，而各分析师节点的 `report` **只在"本轮无
tool_calls"时赋值** ⇒ 该维报告恒为空串（整维证据消失、门控判 F，且原因在交付链路上
不可见）。`run_analyst_turn` 把上限边界接管过来：达上限时不再给工具、追加收尾指令
再问一次，并把截断事件写成 `analyst_truncations`。

覆盖：正常轮 / 未收尾轮 / 达上限收尾 / 收尾失败 / 模型异常返回 tool_calls /
cap 最小值 / 配置三态 / state 累加器 / headless 透出。
"""

import operator
from typing import get_args

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.utils import agent_utils as au
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.default_config import DEFAULT_CONFIG

DEFAULT_CAP = int(DEFAULT_CONFIG["max_tool_rounds_per_analyst"])


class _Runnable:
    """最小可调用替身：`invoke` 返回预置 AIMessage（可指定 tool_calls / 抛错）。"""

    def __init__(self, content="", tool_calls=None, error=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.error = error
        self.seen = None

    def invoke(self, messages):
        self.seen = list(messages)
        if self.error:
            raise self.error
        return AIMessage(content=self.content, tool_calls=self.tool_calls)


class _PromptStub:
    """`prompt | llm` → 预置的收尾 Runnable（用于验证收尾链路确实被走到）。"""

    def __init__(self, runnable):
        self._runnable = runnable

    def __or__(self, other):
        return self._runnable


def _ai_with_tool_call():
    return AIMessage(content="", tool_calls=[{"name": "get_news", "args": {}, "id": "c1"}])


@pytest.fixture()
def cap2(monkeypatch):
    monkeypatch.setattr(au, "_analyst_tool_round_cap", lambda: 2)
    return 2


def _call(chain, wrap=None, messages=None, report_key="sentiment_report"):
    wrap = wrap if wrap is not None else _Runnable(content="收尾正文")
    return au.run_analyst_turn(
        llm=object(),
        prompt=_PromptStub(wrap),
        chain=chain,
        messages=messages if messages is not None else [],
        report_key=report_key,
        analyst_key="social",
    )


# ---------------------------------------------------------------------------
# 正常轮（未达上限）
# ---------------------------------------------------------------------------


def test_below_cap_normal_turn_captures_report(cap2):
    chain = _Runnable(content="正常报告正文")
    out = _call(chain, messages=[AIMessage(content="hi")])

    assert out["sentiment_report"] == "正常报告正文"
    assert isinstance(out["messages"][0], AIMessage)
    assert out["messages"][0].content == "正常报告正文"
    assert not out["messages"][0].tool_calls
    assert "analyst_truncations" not in out, "未截断不该写截断记录"


def test_below_cap_with_pending_tool_calls_keeps_report_empty(cap2):
    """未收尾轮（模型还要调工具）不得写入报告 —— 中间轮写入会把上一轮内容当报告。"""
    chain = _Runnable(content="这是要调工具前的旁白", tool_calls=[{"name": "get_news", "args": {}, "id": "c"}])
    out = _call(chain, messages=[AIMessage(content="hi")])

    assert out["sentiment_report"] == "", "带 tool_calls 的轮次不得写报告"
    assert out["messages"][0].tool_calls, "必须把带 tool_calls 的消息交回图，路由才能继续调工具"
    assert "analyst_truncations" not in out


# ---------------------------------------------------------------------------
# 达上限 → 不带工具的收尾轮
# ---------------------------------------------------------------------------


def test_at_cap_switches_to_tool_free_wrapup(cap2):
    chain = _Runnable(content="不该被调用")
    wrap = _Runnable(content="收尾正文")
    messages = [_ai_with_tool_call(), _ai_with_tool_call()]        # 已用满 2 轮

    out = _call(chain, wrap=wrap, messages=messages)

    assert chain.seen is None, "达上限后不得再走带工具的链路"
    assert out["sentiment_report"].startswith(au.ANALYST_TRUNCATION_MARKER)
    assert "收尾正文" in out["sentiment_report"]
    assert out["analyst_truncations"] == ["social"]


def test_wrapup_message_carries_no_tool_calls(cap2):
    """收尾轮返回的消息必须**不含 tool_calls**，否则路由会继续调工具、无法收口。"""
    out = _call(_Runnable(), wrap=_Runnable(content="x"), messages=[_ai_with_tool_call()] * 2)

    assert isinstance(out["messages"][0], AIMessage)
    assert not out["messages"][0].tool_calls


def test_wrapup_appends_instruction_and_keeps_history(cap2):
    wrap = _Runnable(content="收尾正文")
    messages = [_ai_with_tool_call(), _ai_with_tool_call()]

    _call(_Runnable(), wrap=wrap, messages=messages)

    assert wrap.seen[:len(messages)] == messages, "收尾请求必须带完整历史（否则模型无据可依）"
    assert wrap.seen[-1].content == au.ANALYST_WRAPUP_INSTRUCTION


def test_wrapup_ignores_tool_calls_returned_by_model(cap2):
    """即使收尾轮另返回 tool_calls（异常形态），交给 state 的消息也必须无 tool_calls。"""
    wrap = _Runnable(content="正文", tool_calls=[{"name": "get_news", "args": {}, "id": "z"}])

    out = _call(_Runnable(), wrap=wrap, messages=[_ai_with_tool_call()] * 2)

    assert not out["messages"][0].tool_calls, "必须自行构造不含 tool_calls 的消息以保证收口"


def test_wrapup_failure_records_truncation_and_stays_empty(cap2):
    """收尾调用自身失败：不抛异常、按空报告处理，但截断事件照常记录（不得静默）。"""
    wrap = _Runnable(error=RuntimeError("boom"))

    out = _call(_Runnable(), wrap=wrap, messages=[_ai_with_tool_call()] * 2)

    assert out["sentiment_report"] == ""
    assert out["analyst_truncations"] == ["social"]
    assert not out["messages"][0].tool_calls


# ---------------------------------------------------------------------------
# cap 最小值与正常路径（防 off-by-one 削掉工具能力）
# ---------------------------------------------------------------------------


def test_cap_one_still_allows_first_tool_round(monkeypatch):
    """cap=1 时首轮**仍须能调工具**（收尾只在"下一轮"发生），否则等于禁用工具。"""
    monkeypatch.setattr(au, "_analyst_tool_round_cap", lambda: 1)
    chain = _Runnable(content="", tool_calls=[{"name": "get_news", "args": {}, "id": "c"}])

    first = _call(chain, messages=[])
    assert chain.seen is not None, "cap=1 的首轮应正常调工具"
    assert first["messages"][0].tool_calls

    second = _call(_Runnable(), wrap=_Runnable(content="收尾"), messages=[_ai_with_tool_call()])
    assert second["analyst_truncations"] == ["social"]


# ---------------------------------------------------------------------------
# 轮次计数与配置读取（含 None / 非法三态）
# ---------------------------------------------------------------------------


def test_tool_rounds_used_counts_only_ai_with_tool_calls():
    class _NoAttr:
        pass

    assert au.analyst_tool_rounds_used([]) == 0
    assert au.analyst_tool_rounds_used([AIMessage(content="x")]) == 0
    assert au.analyst_tool_rounds_used([_NoAttr()]) == 0, "缺 tool_calls 属性的消息不得抛错"
    assert au.analyst_tool_rounds_used([_ai_with_tool_call(), AIMessage(content="x")]) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3),
        (None, DEFAULT_CAP),
        ("abc", DEFAULT_CAP),
        (0, 1),            # 非法（须 >=1）→ 夹到 1，不得变成"无上限"
        ("7", 7),
    ],
)
def test_cap_reads_config_with_safe_fallback(monkeypatch, value, expected):
    from tradingagents.dataflows import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "get_config", lambda: {"max_tool_rounds_per_analyst": value})
    assert au._analyst_tool_round_cap() == expected


# ---------------------------------------------------------------------------
# state 累加器与透出
# ---------------------------------------------------------------------------


def test_state_field_uses_add_reducer():
    """无 reducer 时 7 个分析师逐次写入会互相覆盖，只剩最后一个。"""
    annotation = AgentState.__annotations__["analyst_truncations"]
    assert operator.add in get_args(annotation)


def test_create_initial_state_seeds_truncation_list():
    from tradingagents.graph.propagation import Propagator

    state = Propagator().create_initial_state("600519", "2026-09-30")
    assert state["analyst_truncations"] == []


def test_headless_analysis_detail_exposes_truncations():
    from cli.headless import _build_analysis_detail

    assert _build_analysis_detail({"analyst_truncations": ["social"]})["analyst_truncations"] == ["social"]
    # 缺字段 / None 三态：必须降级为 []，不得抛错
    assert _build_analysis_detail({})["analyst_truncations"] == []
    assert _build_analysis_detail({"analyst_truncations": None})["analyst_truncations"] == []
