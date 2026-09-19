"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


# Mirrors the Trader: the schema alone cannot stop the model from putting
# price levels into the prose fields, so the prompt says it explicitly too.
_NO_LEVELS_RULE = (
    "\n- Do NOT state entry prices, stop-loss levels, target prices or "
    "position sizes for this security; give the rating and the reasoning."
)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]
        # 数据质量门控结论（此前只进牛熊研究员）：终裁必须知道哪些报告被判 D/F
        quality = state.get("data_quality_summary", "")

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**A-Stock Trading Constraints — execution & position-sizing rules** (these are NOT evidence against a position):
- These constraints are identical for every A-share name. They tell you HOW a decision must be executed
  (size, entry timing, whether a stop is practically executable) — they are NOT case-specific evidence for
  or against this stock. Never downgrade a rating merely because these constraints exist.
- T+1 settlement: shares bought today cannot be sold until the next trading day
- Daily price limits: main board ±10%, STAR/ChiNext ±20%, Beijing Stock Exchange ±30%.
  Risk-warning stocks (ST/*ST) do NOT get a narrower band: since 2026-07-06 main-board
  ST/*ST moved from ±5% to ±10% (same as ordinary main-board shares), and STAR/ChiNext
  ST/*ST have always been ±20%.
- Newly listed stocks have NO price limit for their first 5 trading days (first day only
  on the Beijing Stock Exchange) — this matters most for recently-IPO'd names.
- Minimum lot size: 100 shares (1 手) on main board and ChiNext, in 100-share multiples;
  STAR board is 200 shares minimum, incrementing by 1 share; Beijing Stock Exchange is
  100 shares minimum, incrementing by 1 share.
- Trading hours (Beijing time): opening call auction 09:15-09:25, continuous trading
  09:30-11:30 and 13:00-14:57, closing call auction 14:57-15:00. Since 2026-07-06 the
  after-hours fixed-price session (15:05-15:30, traded at the closing price) covers all
  A-shares and ETFs.
- ST/delisting risk: ST or *ST status signals regulatory warning; factor into position sizing
- Margin eligibility: not all A-shares are margin-eligible; assume cash-only unless stated

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Rating Calibration** (rate the *balance of evidence*, not the amount of known risk):
- Weigh the decision-relevant dimensions: direction of earnings and cash flow, valuation versus the actual
  growth path, fund-flow / chip structure, verifiable catalysts with a dated path, and policy direction.
- **Hold is a verdict, not a default.** Use Hold only when the bull and bear cases are genuinely balanced
  after weighing evidence quality. "A good company but not a perfect entry" or "risks exist" are NOT
  sufficient grounds for Hold or Underweight.
- **Mandatory positive trigger**: if the bull case prevails on those dimensions while the bear case rests
  mainly on (i) execution constraints (T+1, price limits, stop-loss feasibility) or (ii) the price having
  already risen, you MUST rate **Buy** or **Overweight** — Overweight when 1-2 material risks remain
  unresolved, Buy when the evidence is decisive and valuation is not clearly stretched.
- **Mandatory negative trigger** (symmetric to the above): if the bear case prevails on those dimensions
  while the bull case rests mainly on (i) theme / narrative extrapolation without a verifiable earnings or
  cash-flow path, or (ii) sentiment or momentum alone, you MUST rate **Underweight** or **Sell** —
  Underweight when the deterioration is real but partly priced in, Sell when the evidence is decisive or
  the downside path is unhedgeable.
- Both triggers are symmetric: never let the absence of perfect evidence on one side alone decide the
  rating, in either direction.
- Missing or low-quality data is an uncertainty to disclose — it is **neither** a bearish **nor** a bullish
  argument, and it must not move the rating in either direction.
- Do NOT: follow the most pessimistic analyst; end with "wait for a right-side signal" without giving a
  conditional rating; treat "it has already run up" as a sufficient rejection; let unverifiable
  extrapolation push the rating up either.
- State explicitly which single piece of evidence would flip your rating.

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
- Data quality gate (reports graded D/F are weak evidence — do not build the rating on them):
{quality if quality else "（本次无数据质量门控结论）"}
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{_NO_LEVELS_RULE}{get_language_instruction()}"""

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
