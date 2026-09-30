import logging

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

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


# ---------------------------------------------------------------------------
# 工具轮次上限的**收尾兜底**（0.6.7）
# ---------------------------------------------------------------------------
# 问题：graph 层护栏（`graph/conditional_logic.py`）在单个分析师用满
# `max_tool_rounds_per_analyst` 轮工具调用后结束其工具循环；而各分析师节点的
# `report` **只在"本轮无 tool_calls"时才赋值** ⇒ 护栏若直接收口，该维报告恒为空串：
# 整维证据凭空消失，门控判 F，而**原因在交付链路上不可见**（生产 headless 成功路径
# 丢弃 stderr，只剩一条 logger.warning）。
#
# 实测（2026-09-30 插桩复现，标的 301119）：
#   Tool-call round limit reached (12) before Msg Clear Social
#   [DIAG] after=Msg Clear Social used_rounds=12/12 last_has_tool_calls=True -> TRUNCATED
#   [DIAG] sentiment_report len=0            ← 其余 6 维 2352~6765 字
# 近 9 次生产缓存统计：**7 次**出现整维空报告（情绪维 6 次、游资维 1 次）。
# 触发条件与降级数据源吻合——情绪/游资分析师的取数工具（主力资金、北向、热点榜）
# 在本网络被拦，模型反复重试取数，最易打满轮次上限。
#
# 处理：达上限时**不再提供工具**、再发一次收尾请求，让该维输出"基于已取得数据的
# 报告 + 未取到项如实标注"；同时把截断事件写入 state（`analyst_truncations`），
# 由 headless 透出到 `analysis_detail`、由数据质量门控在摘要里显式提示，
# 使下游归因指向"被护栏截断"而非"数据源缺失"。
logger = logging.getLogger(__name__)

ANALYST_WRAPUP_INSTRUCTION = (
    "【系统指令 · 最后一轮】工具调用轮次已达上限，本轮**不得再调用任何工具**。"
    "请立即基于上文中已经取得的数据，按上述角色要求输出**最终报告正文**："
    "包含结论、关键数值与汇总表格；未能取到的必采项如实写成 `[数据缺失: xxx]`，"
    "不要编造，也不要用新闻语气补一个数字。"
)

# 收尾轮产出的报告**前置声明**：让读者（与下游归因）一眼看出这是被护栏截断收尾，
# 而不是数据源本身没有该维数据。
ANALYST_TRUNCATION_MARKER = (
    "> ⚠️ 本维报告在**工具调用轮次上限**处收尾（护栏截断，非数据源缺失）："
    "以下内容基于截断前已取得的数据；未取到的必采项已如实标注。\n\n"
)


def analyst_tool_rounds_used(messages) -> int:
    """已完成的工具轮次 = 带 `tool_calls` 的 AIMessage 条数。

    与 `graph.conditional_logic.ConditionalLogic._tool_rounds_used` **同一口径**：
    Msg Clear 在每个分析师之间清空 messages，故此刻条数即本阶段已用轮次。
    """
    return sum(1 for m in messages if getattr(m, "tool_calls", None))


def _analyst_tool_round_cap() -> int:
    """`max_tool_rounds_per_analyst`（读运行配置；读不到时回落 `DEFAULT_CONFIG`）。

    与 graph 层护栏取**同一配置键**（`trading_graph.py` 以它构造 ConditionalLogic），
    两边由此保持一致；配置不可读也不该阻断分析师，故一律兜底为默认值。
    """
    value = None
    try:
        from tradingagents.dataflows.config import get_config

        value = get_config().get("max_tool_rounds_per_analyst")
    except Exception:  # noqa: BLE001 —— 配置层异常不得影响分析
        pass
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        from tradingagents.default_config import DEFAULT_CONFIG

        return max(1, int(DEFAULT_CONFIG["max_tool_rounds_per_analyst"]))


def run_analyst_turn(llm, prompt, chain, messages, report_key, analyst_key):
    """执行一次分析师轮次；工具轮次达上限时改为**不带工具**的收尾请求。

    正常运行：调 `chain`（已 `bind_tools`），本轮无 tool_calls 时把正文写入报告字段。

    达上限（`analyst_tool_rounds_used(messages) >= cap`）：本轮**不再给工具**，
    用同一 prompt 追加收尾指令再问一次，把返回正文写入报告字段并前置截断声明；
    返回的消息**保证不含 tool_calls**，graph 的收尾路径据此正常收口（不会留下空报告）。
    收尾调用自身失败时按空报告处理（不抛异常阻断管线），截断事件照常记录。

    返回该分析师节点的 state 片段。
    """
    cap = _analyst_tool_round_cap()
    used = analyst_tool_rounds_used(messages)

    if used >= cap:
        logger.warning(
            "%s: 工具轮次已达上限 %d → 转为不带工具的收尾轮（避免整维报告为空）",
            analyst_key, cap,
        )
        content = ""
        try:
            final = (prompt | llm).invoke(
                list(messages) + [HumanMessage(content=ANALYST_WRAPUP_INSTRUCTION)]
            )
            content = str(getattr(final, "content", "") or "")
        except Exception as exc:  # noqa: BLE001 —— 收尾失败也不得阻断管线
            logger.warning("%s: 收尾轮调用失败（%s）→ 该维按空报告处理", analyst_key, exc)
        return {
            # 消息自行构造（而非透传模型返回）：确保**不带 tool_calls**，路由必然收口
            "messages": [AIMessage(content=content)],
            report_key: (ANALYST_TRUNCATION_MARKER + content) if content else "",
            "analyst_truncations": [analyst_key],
        }

    result = chain.invoke(messages)
    report = (
        str(getattr(result, "content", "") or "")
        if not getattr(result, "tool_calls", None)
        else ""
    )
    return {"messages": [result], report_key: report}


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


        
