"""图流程控制：条件边路由、轮次阈值、工具循环护栏、节点名一致性、配置校验。

此前 `ConditionalLogic` / `setup_graph` / `max_*_rounds` / `max_recur_limit` 在
tests 里**一次都没被引用**，于是这几类问题长期存活而无人察觉：
- 注释写着 "3 rounds of back-and-forth"，实际配置 1 只有 1 个来回；
- 轮数配成 0 时多空辩论静默退化成单边（Bull 必跑、Bear 永不发言），不报错；
- 分析师条件边用列表形式 `[tools_X, clear]`，节点名靠两处字符串手工对齐，写错
  只在运行时抛 "node not found"；
- 工具循环没有 per-analyst 上限，唯一止损是全图 recursion_limit，撞上即整次运行作废。
"""

from unittest.mock import MagicMock

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import _validate_count_configs

ANALYST_KEYS = ("market", "social", "news", "fundamentals", "policy", "hot_money", "lockup")


class _Msg:
    """最小消息替身：条件边只看 `tool_calls`。"""

    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []


def _route(key: str, messages):
    logic = ConditionalLogic()
    return getattr(logic, f"should_continue_{key}")({"messages": messages})


# ---------------------------------------------------------------------------
# 分析师阶段：路由与工具循环护栏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ANALYST_KEYS)
def test_analyst_routes_to_tools_while_tool_calls_pending(key):
    assert _route(key, [_Msg([{"name": "get_stock_data"}])]) == f"tools_{key}"


@pytest.mark.parametrize("key", ANALYST_KEYS)
def test_analyst_routes_to_msg_clear_without_tool_calls(key):
    # 注意 capitalize()：hot_money → "Hot_money"（不是 "Hot_Money"），
    # 这个名字必须与 setup.py 里 add_node 的同名派生完全一致。
    assert _route(key, [_Msg([])]) == f"Msg Clear {key.capitalize()}"


def test_tool_round_limit_truncates_runaway_analyst():
    """达到轮次上限即截断，不再把整张图拖到 recursion_limit。"""
    logic = ConditionalLogic(max_tool_rounds=2)
    # 已用 3 轮（3 条带 tool_calls 的消息），最后一条又要求调用工具
    messages = [_Msg([{"n": 1}]), _Msg([]), _Msg([{"n": 2}]), _Msg([{"n": 3}])]

    assert logic.should_continue_market({"messages": messages}) == "Msg Clear Market"


def test_under_tool_round_limit_keeps_calling_tools():
    logic = ConditionalLogic(max_tool_rounds=3)

    # 已用 2 轮（最后一条仍在要求调用工具）→ 未到上限，继续
    assert logic.should_continue_market(
        {"messages": [_Msg([{"n": 1}]), _Msg([]), _Msg([{"n": 2}])]}
    ) == "tools_market"


def test_tool_round_count_ignores_messages_without_tool_calls():
    logic = ConditionalLogic(max_tool_rounds=2)
    # 5 条普通消息（含 ToolMessage 回执）不算轮次，只有带 tool_calls 的 AIMessage 算
    messages = [_Msg([]) for _ in range(5)] + [_Msg([{"n": 1}])]

    assert logic._tool_rounds_used(messages) == 1
    assert logic.should_continue_market({"messages": messages}) == "tools_market"


# ---------------------------------------------------------------------------
# 辩论 / 风控：阈值语义
# ---------------------------------------------------------------------------


def test_debate_stops_at_two_speeches_per_round():
    """max_debate_rounds=1 → 1 个来回 = Bear + Bull 各一次（count>=2 即收口）。

    0.5.31：发言顺序改为**空方开场、多方收尾**（入口即 Bear，见 `setup.py`）。
    """
    logic = ConditionalLogic(max_debate_rounds=1)

    assert logic.should_continue_debate(
        {"investment_debate_state": {"count": 2, "current_response": "Bull: ..."}}
    ) == "Research Manager"
    assert logic.should_continue_debate(
        {"investment_debate_state": {"count": 1, "current_response": "Bear: ..."}}
    ) == "Bull Researcher"


def test_debate_second_round_alternates():
    logic = ConditionalLogic(max_debate_rounds=2)

    assert logic.should_continue_debate(
        {"investment_debate_state": {"count": 3, "current_response": "Bear: ..."}}
    ) == "Bull Researcher"


def test_debate_order_opens_with_bear_and_bull_closes():
    """0.5.31 不变量：发言序 Bear→Bull→Bear→Bull（**多方收尾**），来回数不变。

    此前是多方开场、空方收尾，与偶数阈值（2×rounds）叠加后空方**永远拿到最后一句话**；
    裁决策略由研究经理给出，而 LLM 对最后读到的论证存在 recency 偏置——实测
    2026-09-21~09-24 的 24 票发言序恒为 Bull→Bear→Bull→Bear，终裁无一条 Buy/Overweight。
    """
    logic = ConditionalLogic(max_debate_rounds=2)
    spoken = ["Bear"]                      # 入口即 Bear（setup.py: Quality Gate → Bear Researcher）
    for _ in range(10):
        nxt = logic.should_continue_debate(
            {"investment_debate_state": {"count": len(spoken),
                                         "current_response": f"{spoken[-1]}: ..."}}
        )
        if nxt == "Research Manager":
            break
        spoken.append(nxt.split()[0])
    else:                                   # pragma: no cover - 防死循环
        raise AssertionError(f"debate never converged: {spoken}")

    assert spoken == ["Bear", "Bull", "Bear", "Bull"]     # 多方收尾
    assert len(spoken) == 2 * logic.max_debate_rounds     # 来回数不变（2 个来回）


def test_risk_discussion_stops_at_three_speeches_per_round():
    """max_risk_discuss_rounds=1 → 1 个循环 = A → C → N（count>=3 即收口）。"""
    logic = ConditionalLogic(max_risk_discuss_rounds=1)

    assert logic.should_continue_risk_analysis(
        {"risk_debate_state": {"count": 3, "latest_speaker": "Neutral"}}
    ) == "Portfolio Manager"
    assert logic.should_continue_risk_analysis(
        {"risk_debate_state": {"count": 1, "latest_speaker": "Aggressive"}}
    ) == "Conservative Analyst"
    assert logic.should_continue_risk_analysis(
        {"risk_debate_state": {"count": 2, "latest_speaker": "Conservative"}}
    ) == "Neutral Analyst"


# ---------------------------------------------------------------------------
# 节点名一致性：条件边返回的字符串必须真的存在
# ---------------------------------------------------------------------------


def test_analyst_routing_targets_exist_in_graph():
    """把条件边返回的节点名与 setup_graph 注册的名字绑起来。

    分析师阶段的条件边用的是列表形式，写错名字没有静态检查。
    """
    tool_nodes = {key: MagicMock() for key in ANALYST_KEYS}
    logic = ConditionalLogic()
    setup = GraphSetup(
        MagicMock(), MagicMock(), tool_nodes=tool_nodes, conditional_logic=logic
    )
    workflow = setup.setup_graph(list(ANALYST_KEYS))
    nodes = set(workflow.nodes)

    for key in ANALYST_KEYS:
        assert f"tools_{key}" in nodes, f"缺少节点 tools_{key}"
        assert f"Msg Clear {key.capitalize()}" in nodes, f"缺少节点 Msg Clear {key.capitalize()}"
        for target in (_route(key, [_Msg([{"n": 1}])]), _route(key, [_Msg([])])):
            assert target in nodes, f"{key} 的条件边指向了不存在的节点 {target!r}"


def test_debate_and_risk_targets_exist_in_graph():
    tool_nodes = {key: MagicMock() for key in ANALYST_KEYS}
    logic = ConditionalLogic()
    setup = GraphSetup(
        MagicMock(), MagicMock(), tool_nodes=tool_nodes, conditional_logic=logic
    )
    nodes = set(setup.setup_graph(list(ANALYST_KEYS)).nodes)

    for target in ("Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
                   "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
                   "Portfolio Manager", "Quality Gate"):
        assert target in nodes, f"缺少节点 {target}"


# ---------------------------------------------------------------------------
# 计数值配置校验
# ---------------------------------------------------------------------------

_COUNT_KEYS = (
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "max_tool_rounds_per_analyst",
    "memory_holding_days",
    "memory_min_holding_days",
)


def test_default_config_passes_validation():
    _validate_count_configs(DEFAULT_CONFIG)


def test_default_config_declares_every_validated_key():
    """校验的键必须在 DEFAULT_CONFIG 里——否则校验在真实运行时被跳过。"""
    for key in _COUNT_KEYS:
        assert key in DEFAULT_CONFIG, f"DEFAULT_CONFIG 缺少 {key}"


@pytest.mark.parametrize("key", _COUNT_KEYS)
@pytest.mark.parametrize("bad", [0, -1])
def test_zero_or_negative_count_is_rejected(key, bad):
    config = dict(DEFAULT_CONFIG)
    config[key] = bad

    with pytest.raises(ValueError) as exc:
        _validate_count_configs(config)

    assert key in str(exc.value)


@pytest.mark.parametrize("key", _COUNT_KEYS)
def test_non_integer_count_is_rejected(key):
    config = dict(DEFAULT_CONFIG)
    config[key] = "many"

    with pytest.raises(ValueError) as exc:
        _validate_count_configs(config)

    assert key in str(exc.value)
