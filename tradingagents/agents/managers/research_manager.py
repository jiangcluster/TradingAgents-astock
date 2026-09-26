"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")
    # 第二通道：推理档拒绝 tool_choice，json_mode 仍可用（0.5.33）
    json_structured = bind_structured(llm, ResearchPlan, "Research Manager", json_mode=True)

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")
        # 数据质量门控结论：此前只进牛熊两个研究员，研究经理看不到——
        # 导致 investment_plan 可能建立在被判 D/F 的报告之上而无任何提示。
        quality = state.get("data_quality_summary", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

Note: This is an A-share (China mainland) stock. Factor in regulatory policy impact, hot money / capital flow dynamics, and lockup expiry / insider reduction risks when synthesising the debate.

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

**Rating Calibration** (rate the *balance of evidence*, not the amount of known risk):
- Weigh the decision-relevant dimensions: direction of earnings and cash flow, valuation versus the actual
  growth path, fund-flow / chip structure, verifiable catalysts with a dated path, and policy direction.
- **Hold is a verdict, not a default.** "A good company but not a perfect entry" or "risks exist" are NOT
  sufficient grounds for Hold or Underweight.
- **Execution constraints are not case-specific evidence.** T+1 settlement, daily price limits and lot/board
  rules apply identically to every A-share name; they constrain HOW a position is sized and entered, and must
  NOT by themselves move the rating down. If the bear case rests mainly on them, or mainly on "the price has
  already run up", while the bull case prevails on the decision-relevant dimensions, you MUST rate **Buy** or
  **Overweight** — Overweight when 1-2 material risks remain unresolved, Buy when the evidence is decisive
  and valuation is not clearly stretched.
- **Mandatory negative trigger** (symmetric): if the bear case prevails on those dimensions while the bull
  case rests mainly on theme / narrative extrapolation without a verifiable earnings or cash-flow path, or on
  sentiment or momentum alone, you MUST rate **Underweight** or **Sell**.
- **Missing or low-quality data is an uncertainty to disclose — it is neither a bearish nor a bullish
  argument.** Do not let a D/F grade strip the bull's evidence while leaving the bear's case intact: judge
  whichever side still has usable evidence, and say which claims became unverifiable.
- **The last speaker is not automatically the stronger side** — weigh the arguments themselves, not the order
  in which they were delivered.
- State explicitly which single piece of evidence would flip your rating.

**Data Quality Context** (weigh it when judging arguments; a claim built on a D/F report is weak evidence —
but missing data is not evidence *for* the other side either):
{quality if quality else "（本次无数据质量门控结论）"}

---

**Debate History:**
{history}""" + get_language_instruction()

        rendered = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
            json_structured=json_structured,
            schema=ResearchPlan,
        )
        investment_plan = rendered.text

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
