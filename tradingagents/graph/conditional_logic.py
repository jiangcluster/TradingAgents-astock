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
        阶段，离真实成因很远。达到上限即截断该分析师（报告可能因此为空，质量门控
        会判 D/F），并在日志里说明。
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
        if used >= self.max_tool_rounds:
            logger.warning(
                "Tool-call round limit reached (%d) before %s; truncating this analyst "
                "instead of letting the loop run into the graph-wide recursion limit. "
                "Its report may be empty or partial — the quality gate will grade it "
                "accordingly.",
                self.max_tool_rounds, clear_node,
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
        """Determine if debate should continue."""

        # count 每次发言 +1。max_debate_rounds 的语义是"多空来回数"：一个来回 =
        # 2 次发言（Bull + Bear），故阈值为 2×。原注释写"3 rounds of back-and-forth"
        # 是上游 max_*=3 时代留下的，与当前配置语义不符（配置 1 → 实际 1 个来回）。
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
