

def create_conservative_debator(llm):
    def conservative_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        lockup_report = state.get("lockup_report", "")

        trader_decision = state["trader_investment_plan"]

        prompt = f"""As the Conservative Risk Analyst evaluating an A-share (China mainland) stock, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. Critically examine high-risk elements in the trader's plan, pointing out where it may expose the firm to undue risk.

A-Share Conservative Framework — emphasize these China-specific downside risks:
- T+1 Settlement Lock: Any position taken today CANNOT be exited until tomorrow. If the stock gaps down at open (e.g. after overnight policy news or global sell-off), losses are locked in with no recourse. It constrains HOW a position is sized and entered — it does not by itself decide WHETHER the opportunity is worth taking.
- Daily Price Limit Trap (涨跌停板): If a stock hits limit-down (main board -10%, STAR/ChiNext -20%, Beijing Stock Exchange -30%), the order book on the buy side is typically empty, so sell orders queue but rarely fill. Since 2026-07-06 the after-hours fixed-price session (15:05-15:30, at the closing price) covers all A-shares, so exiting is not strictly impossible — but it still depends on finding a counterparty, which is exactly what is missing on a limit-down day. Treat it as "effectively trapped", not "literally unable to place an order". Multiple consecutive limit-downs can cause catastrophic losses with no practical ability to exit.
- Lockup Expiry Overhang: Large lockup expiries (限售解禁) create massive potential sell pressure. Even if insiders haven't started selling, the OPTION to sell depresses sentiment and caps upside.
- Policy Reversal Risk: A-shares are a policy market (政策市). What the government gives, it can take away overnight — sector support can turn to sector crackdown with a single State Council directive.
- Hot Money Exit Risk (游资撤退): Hot money moves fast in both directions. Today's limit-up star is tomorrow's limit-down casualty. Retail investors are the last to know when hot money exits.
- Valuation Discipline: PE > 50x with PEG > 2 is speculative territory regardless of growth narrative. The 30x PE digestion framework should be the anchor — if it takes 5+ years to digest, the position is overvalued.
- ST/Delisting Risk: For companies with consecutive losses, ST designation signals regulatory risk warning, restricts which investors may buy (a risk-warning-board permission is required), removes the stock from margin-trading eligibility, and often triggers institutional forced selling. Note it does NOT narrow the daily band: main-board ST/*ST is ±10% since 2026-07-06, and STAR/ChiNext ST/*ST is ±20%. The danger is the delisting path and the shrinking buyer pool, not a tighter price limit.

Here is the trader's decision:

{trader_decision}

Counter the aggressive and neutral analysts. Highlight where their optimism overlooks A-share structural risks. Use these data sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest News Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Policy Analysis Report: {policy_report}
Hot Money / Capital Flow Report: {hot_money_report}
Lockup Expiry / Insider Reduction Report: {lockup_report}
Conversation history: {history} Last aggressive argument: {current_aggressive_response} Last neutral argument: {current_neutral_response}. If no responses yet, present your own argument.

Separate two kinds of argument, and say explicitly which one you are making:
(1) Execution constraints (T+1, price limits, lot/board rules): these apply to every A-share name, so they can
only justify a SMALLER size, a staged entry or a longer holding tolerance — they are NOT a reason to reject
this stock. If this is your main case, recommend the sizing/execution change instead of rejection.
(2) Company-specific bearish evidence (earnings or cash-flow deterioration, distribution (派发) structure, a
valuation the actual growth path cannot digest, policy / legal / lockup events, insider selling): quantify each
one with the data you have and state what would resolve or falsify it. Only when (2) genuinely prevails should
you argue against taking exposure at all.
Judging evidence quality on both sides is part of your job: "the bull case relies on unverifiable
extrapolation" is a strong argument; "A-shares are structurally risky" is not, because it is true of every
name. Output conversationally without special formatting."""

        response = llm.invoke(prompt)

        argument = f"Conservative Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
