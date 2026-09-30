import logging
import os

logger = logging.getLogger(__name__)

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")


def _env_int(name: str, default=None):
    """读整型环境变量；**缺失/空/非法一律回落默认**（0.5.34 / 批 L）。

    此前 `int(os.environ["TRADINGAGENTS_MAX_TOKENS"]) if os.environ.get(...) else None`
    只防了"没设"，**没防"设错"**：`TRADINGAGENTS_MAX_TOKENS=abc` 会让
    `import tradingagents.default_config` 直接抛 `ValueError` → 整个引擎不可用，
    且报错栈与"环境变量"无关（与 `a_stock._env_number` 修的是同一类坑，那是第二处）。
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 非法（应为整数）→ 忽略，改用默认值 %r",
                       name, raw, default)
        return default


DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # 记忆结算的持有窗口与最低结算门槛（单位：交易日）。
    # 结算口径的历史问题：原先只要拿到 ≥2 行行情就结算，昨天做的决策今天再跑同一只票
    # 会用 1 日收益冒充持有期收益，反思与绩效统计随之失真。现要求至少
    # memory_min_holding_days 个交易日过去才结算；实际持有天数取
    # min(memory_holding_days, 已过去交易日数)。
    "memory_holding_days": 5,
    "memory_min_holding_days": 5,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # 单次回复的最大输出 token 数。None = 用 provider 自己的默认值。
    # 报告写到一半就断，通常就是撞了这个上限（不是上下文超长）——把它调大即可（#91）。
    # 走 anthropic 通道跑**第三方模型**（Kimi 等）时尤其要注意：langchain-anthropic
    # 认不出这些模型名，会落到一个很小的兜底值，所以 anthropic 客户端对非 Claude
    # 模型自带一个更宽的默认值，见 llm_clients/anthropic_client.py。
    "max_tokens": _env_int("TRADINGAGENTS_MAX_TOKENS"),
    # 单次 LLM 请求超时（秒）。None = 用 provider/客户端默认值。设具体值可兜底
    # 「请求挂起导致静默卡死」——超时后由客户端抛异常，而非进程 alive 但永久无输出。
    # 经 `_get_provider_kwargs → _PASSTHROUGH_KWARGS` 透传给 openai / anthropic 系客户端。
    # ⚠️ 本地 fork 说明（T1 移植自源仓库 0.5.19 / PR #100）：150 是"单次 HTTP 请求"预算，
    #   本管线由 LangGraph 逐轮驱动 ReAct 循环、每轮各拿一份完整预算；若实测单轮
    #   deepseek-flash 耗时接近 150s，应通过 `--config-json` 调大而非改本默认值。
    "llm_timeout": 150,
    # 5xx（502/503 等）及读超时后的应用层重试次数。SDK 层恒 0 重试（仅限 OpenAI 兼容
    # 客户端，见 `_resilience_kwargs`），由 `openai_client.invoke` 按下面的冷静期退避重试。
    "llm_max_retries": 3,
    # 重试退避的初始值（秒），指数翻倍：第 1 次重试等 5s、第 2 次 10s、第 3 次 20s...
    # 避免对刚报错的上游立即重试造成雪崩，也避免固定间隔在持续故障时反复撞击。
    "llm_retry_delay": 5,
    # 可选：给单个角色单独指定模型（#39）。留空 = 全部角色沿用上面的
    # quick/deep 两档，行为与以前完全一致——大多数人只有一家模型，不需要碰这里。
    #
    # 用途：让多空辩手用**不同厂商**的模型。同一个模型分饰多角时倾向于互相附和，
    # 换成不同底座才会真的出现反驳。例：
    #   "role_llms": {
    #       "bull": {"provider": "deepseek", "model": "deepseek-chat"},
    #       "bear": {"provider": "qwen",     "model": "qwen-plus"},
    #   }
    # provider 省略则沿用 llm_provider；合法角色名见 graph/setup.py 的 ROLE_KEYS。
    "role_llms": {},
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # ── Claude Agent SDK provider（走个人 Pro/Max 订阅额度，可选依赖 [agentsdk]）──
    # 与内置 anthropic provider 的区别：anthropic 走 ANTHROPIC_API_KEY = **按 token 计费**；
    # 本 provider 走本机已登录的 claude CLI = **消耗订阅额度，不产生 API 账单**。
    # 设为 "claude_agent_sdk" 时，仅 deep_thinking_llm 节点（Research Manager /
    # Portfolio Manager）走订阅。None = 维持原行为。
    "deep_think_provider_override": None,
    # 同上，作用于 QUICK 节点（7 个工具分析师 + 多空/交易员/风险辩手）。
    # 与上一项同时开启 = 全节点走订阅。None = 分析师仍走 llm_provider（维持原行为）。
    "quick_think_provider_override": None,
    # Agent SDK 使用的 Claude 模型。**必须是真实 Claude 模型**，不要复用 deep_think_llm。
    # 用别名而非写死版本号：claude CLI 的 "opus"/"sonnet" 恒指向最新模型，
    # 写死 "claude-opus-4-8" 这类 ID 会随版本迭代过期。
    "agent_sdk_model": "opus",
    # QUICK/分析师节点用的 Claude 模型。默认 sonnet 而非 opus——quick 节点数量多
    # （7 分析师 + 辩手），订阅是按额度限流的，全用 opus 很快会撞到上限。
    "agent_sdk_quick_model": "sonnet",
    # 订阅调用失败 / 撞额度时的兜底。None → 回落到 llm_provider + deep_think_llm。
    "agent_sdk_fallback_provider": None,
    "agent_sdk_fallback_model": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Persist the full final-state JSON per run (results_dir/<ticker>/
    # TradingAgentsStrategy_logs/full_states_log_<date>.json). Headless /
    # automation callers turn this off so disk is not filled by one large
    # JSON per analysed ticker.
    "persist_state_log": True,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "Chinese",
    # How many days of price/indicator history the market analyst covers
    # (the "analysis window", ending at the analysis date). Drives the
    # look_back_days the market analyst passes to get_stock_data /
    # get_indicators. The Web sidebar / CLI derive this from a user-picked
    # start date (default: first day of the current month → "monthly" view);
    # None keeps the previous behaviour (the model's own default, ~30). (#16)
    "market_lookback_days": None,
    # Debate and discussion settings
    # max_debate_rounds：多空"来回"数（1 个来回 = Bull + Bear 各发言一次）。
    # max_risk_discuss_rounds：三方风控的"循环"数（1 个循环 = A → C → N 各发言一次）。
    # ⚠️ headless JSON 的 risk_debate/investment_debate 里那个 `rounds` 字段是**发言次数**
    # （count），不是这里的配置值：配置 1 对应多空 rounds=2、风控 rounds=3。两者单位不同。
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # 单个分析师的工具调用轮次上限（0/负数会让分析师的工具循环失去唯一护栏）。
    # 正常使用是 1-3 轮；模型反复调同一工具时此前没有上限，只能撞全图 recursion_limit。
    "max_tool_rounds_per_analyst": 12,
    # 决策节点（Trader / Portfolio Manager）引用**单份分析师报告**的截断上限（字符数）。
    # 0.5.38：此前 Trader 里散着 1500/2000/1200 三个硬编码数字、PM 完全不给原始报告，
    # 统一到本项后"终裁能看到的证据量"可集中调整（调大 = 提示词与成本同步上升）。
    # 见 agents/utils/prompt_clip.py。
    "evidence_clip_chars": 1500,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "a_stock",        # Options: a_stock, alpha_vantage, yfinance
        "technical_indicators": "a_stock",   # Options: a_stock, alpha_vantage, yfinance
        "fundamental_data": "a_stock",       # Options: a_stock, alpha_vantage, yfinance
        "news_data": "a_stock",              # Options: a_stock, alpha_vantage, yfinance
        "signal_data": "a_stock",            # A-stock only: topic attribution, capital flow, consensus
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
