# TradingAgents/graph/conditional_logic.py

import logging

from tradingagents.agents.utils.agent_states import AgentState

logger = logging.getLogger(__name__)


class ConditionalLogic:
    """Handles conditional logic for determining graph flow."""

    def __init__(
        self,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        max_tool_rounds=12,
    ):
        """Initialize with configuration parameters.

        ``max_tool_rounds``：单个分析师的工具调用轮次上限。此前**没有任何上限**——
        模型反复调同一个工具时唯一兜底是全图共享的 recursion_limit，撞上就抛
        GraphRecursionError、整次运行的结果全部丢弃，而且报错位置常常在辩论/风控
        阶段，离真实成因很远。

        **上限边界的归属（0.6.7 订正）**：达到上限时**不是**在这里直接收口，而是由
        分析师节点自己转入「不带工具的收尾轮」（`agents/utils/agent_utils.run_analyst_turn`），
        因为各节点的 `report` 只在"本轮无 tool_calls"时才赋值——护栏若直接收口，该维报告
        恒为空串（整维证据丢失，且原因在交付链路上不可见）。实测 2026-09-30：近 9 次
        生产运行有 **7 次**出现整维空报告。

        因此本方法保留 `used > max_tool_rounds` 作为**极端兜底**：正常流程下节点在
        `used == max_tool_rounds` 时即转入收尾轮、不再产生 tool_calls，故这里不会命中；
        只有当某个分析师节点未按约定收尾（例如后续新增角色忘记走 `run_analyst_turn`、
        收尾轮仍返回 tool_calls）时才会触发——它比旧行为只多容忍一轮，随后照旧硬截断，
        不会退化成无限循环。
        """
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds
        self.max_tool_rounds = max_tool_rounds

    @staticmethod
    def _tool_rounds_used(messages) -> int:
        """当前分析师阶段已用掉的工具轮次。

        Msg Clear 在每个分析师之间清空 messages，所以此刻 messages 里的
        "带 tool_calls 的 AIMessage"条数就等于本阶段已完成的工具轮次——不需要
        额外维护状态字段。
        """
        return sum(1 for m in messages if getattr(m, "tool_calls", None))

    def _route_after_analyst(self, state: AgentState, tools_node: str, clear_node: str) -> str:
        messages = state["messages"]
        if not getattr(messages[-1], "tool_calls", None):
            return clear_node
        used = self._tool_rounds_used(messages)
        if used > self.max_tool_rounds:
            logger.warning(
                "Tool-call round limit exceeded (%d > %d) before %s: this analyst ignored "
                "the wrap-up contract (see agents/utils/agent_utils.run_analyst_turn); "
                "truncating it instead of letting the loop run into the graph-wide "
                "recursion limit. Its report may be empty — the quality gate will grade "
                "it accordingly.",
                used, self.max_tool_rounds, clear_node,
            )
            return clear_node
        return tools_node

    def should_continue_market(self, state: AgentState):
        """Determine if market analysis should continue."""
        return self._route_after_analyst(state, "tools_market", "Msg Clear Market")

    def should_continue_social(self, state: AgentState):
        """Determine if social media analysis should continue."""
        return self._route_after_analyst(state, "tools_social", "Msg Clear Social")

    def should_continue_news(self, state: AgentState):
        """Determine if news analysis should continue."""
        return self._route_after_analyst(state, "tools_news", "Msg Clear News")

    def should_continue_fundamentals(self, state: AgentState):
        """Determine if fundamentals analysis should continue."""
        return self._route_after_analyst(state, "tools_fundamentals", "Msg Clear Fundamentals")

    def should_continue_policy(self, state: AgentState):
        """Determine if policy analysis should continue."""
        return self._route_after_analyst(state, "tools_policy", "Msg Clear Policy")

    def should_continue_hot_money(self, state: AgentState):
        """Determine if hot money tracking should continue."""
        return self._route_after_analyst(state, "tools_hot_money", "Msg Clear Hot_money")

    def should_continue_lockup(self, state: AgentState):
        """Determine if lockup/reduction analysis should continue."""
        return self._route_after_analyst(state, "tools_lockup", "Msg Clear Lockup")

    def should_continue_debate(self, state: AgentState) -> str:
        """Determine if debate should continue.

        **发言顺序：空方开场、多方收尾（0.5.31 起）**。此前是多方开场、**空方收尾**，
        与 `max_debate_rounds` 的偶数阈值（2×）叠加后，空方**永远拿到最后一句话**；
        而裁决策略由研究经理给出，LLM 对"最后读到的论证"存在 recency 偏置——
        实测 2026-09-21~09-24 的 24 票发言序恒为 Bull→Bear→Bull→Bear，末句常是
        决定性修辞（"投资最贵的不是错过，而是接一把正在下跌的飞刀"），24 票终裁
        无一条 Buy/Overweight。改为 Bear→Bull→Bear→Bull：来回数不变（仍 2×rounds），
        仅把最后发言权交给多方。
        """
        # count 每次发言 +1。max_debate_rounds 的语义是"多空来回数"：一个来回 =
        # 2 次发言（Bull + Bear），故阈值为 2×。
        if state["investment_debate_state"]["count"] >= 2 * self.max_debate_rounds:
            return "Research Manager"
        if state["investment_debate_state"]["current_response"].startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """Determine if risk analysis should continue."""
        # 同理：max_risk_discuss_rounds 是"三方循环数"，一个循环 = 3 次发言
        # （Aggressive → Conservative → Neutral），故阈值为 3×。
        if state["risk_debate_state"]["count"] >= 3 * self.max_risk_discuss_rounds:
            return "Portfolio Manager"
        if state["risk_debate_state"]["latest_speaker"].startswith("Aggressive"):
            return "Conservative Analyst"
        if state["risk_debate_state"]["latest_speaker"].startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"
