"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)

# The schema alone cannot stop the model from putting price levels into the
# free-text reasoning field, so the prompt says it explicitly too.
_NO_LEVELS_INSTRUCTION = (
    "Explain the reasoning behind the direction. Do NOT state entry prices, "
    "stop-loss levels, target prices or position sizes for this security."
)


def _clip(text: str, limit: int) -> str:
    """截断长报告（Trader 只需决策相关要点，避免提示词被无关篇幅挤爆）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（已截断，完整内容见分析师报告）"


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")
    # 第二通道：推理档拒绝 tool_choice，json_mode 仍可用（0.5.33）
    json_structured = bind_structured(llm, TraderProposal, "Trader", json_mode=True)

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_plan = state["investment_plan"]

        # Collect A-stock specific analyst reports
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        lockup_report = state.get("lockup_report", "")

        # Build optional A-stock context block
        astock_context_parts = []
        if policy_report:
            astock_context_parts.append(f"Policy Analysis Report:\n{policy_report}")
        if hot_money_report:
            astock_context_parts.append(f"Hot Money / Capital Flow Report:\n{hot_money_report}")
        if lockup_report:
            astock_context_parts.append(f"Lockup Expiry / Insider Reduction Report:\n{lockup_report}")
        astock_context = "\n\n".join(astock_context_parts)

        # 关键证据（截断）：此前 Trader 只拿到研究计划 + 上述三份报告，
        # 技术面/基本面原文与整段多空辩论**从未进入"决定买/卖"这一步**，
        # 提示词却声称"based on a comprehensive analysis by a team of analysts"。
        evidence_parts = []
        market_report = _clip(state.get("market_report", ""), 1500)
        fundamentals_report = _clip(state.get("fundamentals_report", ""), 1500)
        if market_report:
            evidence_parts.append(f"Market / Technical Report:\n{market_report}")
        if fundamentals_report:
            evidence_parts.append(f"Fundamentals Report:\n{fundamentals_report}")
        debate_history = _clip(
            state.get("investment_debate_state", {}).get("history", ""), 2000
        )
        if debate_history:
            evidence_parts.append(f"Bull/Bear Research Debate (truncated):\n{debate_history}")
        quality = state.get("data_quality_summary", "")
        if quality:
            evidence_parts.append(f"Data Quality Gate:\n{_clip(quality, 1200)}")
        evidence_context = "\n\n".join(evidence_parts)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent specialising in A-share (China mainland) stocks. "
                    "Translate the Research Manager's investment plan into a structured "
                    "transaction view. You must factor in A-stock trading constraints:\n"
                    "- T+1 settlement: shares bought today cannot be sold until the next trading day\n"
                    "- Daily price limits: main board ±10%, STAR/ChiNext ±20%, Beijing Stock "
                    "Exchange ±30%. ST/*ST does NOT narrow the band — main-board ST/*ST moved "
                    "from ±5% to ±10% on 2026-07-06, and STAR/ChiNext ST/*ST have always been ±20%\n"
                    "- Newly listed stocks have no price limit for their first 5 trading days "
                    "(Beijing Stock Exchange: first day only)\n"
                    "- Minimum lot: 100 shares on main board and ChiNext (100-share multiples); "
                    "STAR board 200 shares minimum (1-share increments); Beijing Stock Exchange "
                    "100 shares minimum (1-share increments)\n"
                    "- Trading hours (Beijing time): call auction 09:15-09:25, continuous "
                    "09:30-11:30 / 13:00-14:57, closing auction 14:57-15:00, after-hours "
                    "fixed-price session 15:05-15:30 (all A-shares since 2026-07-06)\n"
                    "Anchor your reasoning in the analysts' reports and the research plan; if the "
                    "key evidence below contradicts the plan, say so in the reasoning instead of "
                    "silently following it. "
                    f"{_NO_LEVELS_INSTRUCTION} "
                    "（以上参数仅供技术研究参考，不构成投资建议）"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive analysis by a team of analysts (market, "
                    f"sentiment, news, fundamentals, policy, capital flow, and lockup/reduction "
                    f"specialists), here is an investment plan for {company_name}.\n\n"
                    f"{instrument_context}\n\n"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    + (f"Key Analyst Evidence (truncated):\n{evidence_context}\n\n" if evidence_context else "")
                    + (f"Additional A-Stock Analyst Context:\n{astock_context}\n\n" if astock_context else "")
                    + "Leverage these insights to craft the transaction view."
                    + get_language_instruction()
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
            json_structured=json_structured,
            schema=TraderProposal,
        ).text

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
