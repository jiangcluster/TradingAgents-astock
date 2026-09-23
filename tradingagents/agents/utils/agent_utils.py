from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)
from tradingagents.agents.utils.signal_data_tools import (
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)


# 交付格式约束（0.5.30）：下游会把 agent 正文**原样渲染进交付报告**，而模型的"草稿式
# 推理"（逐个罗列成交量再手算、试算试错、"让我…"自我叙述）会一并进报告 → 读者端全是
# 无效信息（实测 2026-09-22 报告 6529 行里 121 行含"让我"、28 行是明确的中间计算）。
# 与语言约束放在同一函数：该函数已被**全部产出型 agent** 调用（7 分析师 + trader +
# 研究经理 + 投组经理 + 5 辩论 agent），合并返回可避免新增 15 处调用面。
_OUTPUT_STYLE = (
    " 交付格式约束（强制，读者只会看到你的输出正文）："
    "只输出结论与关键数值，**禁止**输出中间推导过程——"
    "不得把原始数据逐个罗列后逐项求和、不得展示试算/试错、"
    "不得出现“让我列出/让我计算/首先，让我/求和：/平均值=(…)/N=”这类自我叙述；"
    "需要聚合量时直接给结果 + 一行口径（例：近5日均量 1474 万股 vs 近20日 1705 万股），"
    "不要自行逐个累加；原始数据只用 Markdown 表格呈现关键指标，不粘贴长数组。"
)


def get_language_instruction() -> str:
    """产出型 agent 的**输出约束**：交付格式（恒定）+ 语言（按配置）。

    交付格式约束恒定注入——agent 正文会原样进交付报告，"草稿式推理"与"贴原始数组"
    属无效信息（见 `_OUTPUT_STYLE` 的实测依据）。

    语言约束（0.5.29）：`output_language=Chinese` 时注入中文指令；设为 `English`
    （默认）时**不注入**（省 token）。辩论类 agent 一并纳入（此前刻意留英文 →
    交付报告出现大段英文）；取舍与回退方式见 CHANGELOG 0.5.29。
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    lang_part = "" if lang.strip().lower() == "english" else f" Write your entire response in {lang}."
    return _OUTPUT_STYLE + lang_part


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`). "
        "When a tool argument is named `ticker`, pass only this ticker value; "
        "do not pass company names, sectors, concepts, or search keywords."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
