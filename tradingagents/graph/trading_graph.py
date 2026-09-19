# TradingAgents/graph/trading_graph.py

import logging
import hashlib
import os
from pathlib import Path
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

from langgraph.prebuilt import ToolNode

from tradingagents.llm_clients import create_llm_client

from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.rating import SOURCE_FALLBACK
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.dataflows.config import set_config

# Import the new abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_transactions,
    get_global_news,
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .setup import ROLE_KEYS, GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor

# 七个分析师角色——它们受 `selected_analysts` 控制，没选中就不会进图。
_ANALYST_ROLES = frozenset({
    "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup",
})

# 各家 provider 私有的参数：换了 provider 就不能带过去（别家可能直接拒收）。
_PROVIDER_SPECIFIC_KWARGS = frozenset({
    "reasoning_effort",   # openai
    "thinking_level",     # google
    "effort",             # anthropic
})


def _require_positive_int(config: Dict[str, Any], key: str, why: str) -> int:
    """配置里的计数值必须是 ≥1 的整数，否则**启动即报错**。

    这些值直接决定流程走几步/隔多久结算。0 或负数不会报错，只会让某段流程
    静默消失（例如轮数=0 时 Bear 永远不发言、门槛=0 时 1 日收益被当成持有期收益），
    而运行结果看起来完全正常——所以宁可启动失败，也不要静默退化。
    """
    value = config.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer, got {value!r}. {why}")
    if parsed < 1:
        raise ValueError(f"{key} must be >= 1, got {parsed}. {why}")
    return parsed


def _validate_count_configs(config: Dict[str, Any]) -> None:
    """启动时校验所有"计数值"配置（0/负数会让整段流程静默消失）。

    这些值不参与计算，只决定流程走几步 / 隔多久结算。设成 0 不会报错，只会让某段
    流程悄悄消失（轮数=0 → 空头研究员永不发言；门槛=0 → 1 日收益被当成持有期收益），
    而运行结果看起来完全正常。宁可启动失败，也不要静默退化。
    """
    _require_positive_int(
        config, "max_debate_rounds",
        "0 会让空头研究员永不发言（多空辩论退化成单边陈述）——"
        "质量门控到空头研究员之间的边是无条件边，Bull 必然先跑一次。",
    )
    _require_positive_int(
        config, "max_risk_discuss_rounds",
        "0 会让保守/中立两位风控分析师永不发言（三方风控只剩激进一方）。",
    )
    _require_positive_int(
        config, "max_tool_rounds_per_analyst",
        "它是单个分析师工具循环的唯一护栏，缺了只能撞全图 recursion_limit。",
    )
    _require_positive_int(
        config, "memory_min_holding_days",
        "否则未满持有期的决策会按过短的窗口结算，反思与绩效口径失真。",
    )
    _require_positive_int(
        config, "memory_holding_days",
        "它是结算时采用的持有窗口（交易日）。",
    )


def merge_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """用户传入的 config 是**覆盖项**，不是完整配置：缺的键一律取 DEFAULT_CONFIG。

    README 快速开始的示例只给 4 个键（llm_provider / 两个模型 / output_language）。
    此前这里是 ``config or DEFAULT_CONFIG`` —— 整体替换、不合并，紧接着
    ``os.makedirs(config["data_cache_dir"])`` 就 KeyError，照 README 粘贴即崩（#101）。
    dataflows 层的 set_config() 本来就是合并语义，这里与之对齐。
    浅合并：嵌套字典（如 role_llms）按用户给的整份为准。
    """
    merged = DEFAULT_CONFIG.copy()
    if config:
        merged.update(config)
    return merged


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = merge_config(config)
        self.callbacks = callbacks or []
        # 本次实际进图的分析师键：注入状态供数据质量门控只对已运行者判级（见 quality_gate）
        self.selected_analysts = list(selected_analysts or [])

        # 计数值配置一律在启动时校验（0/负数会让整段流程静默消失）
        _validate_count_configs(self.config)

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        # Optional: route nodes through a personal Claude Pro/Max subscription
        # via the Claude Agent SDK. `deep_think_provider_override` covers the
        # deep nodes (Research / Portfolio Manager); `quick_think_provider_override`
        # covers the quick nodes (7 tool-using analysts + Bull/Bear / trader /
        # risk debaters). Both on ⇒ every node runs on the subscription. Off by
        # default — behaviour is unchanged when both are None.
        deep_on = self.config.get("deep_think_provider_override") == "claude_agent_sdk"
        quick_on = self.config.get("quick_think_provider_override") == "claude_agent_sdk"

        # F-004 guardrail: ANTHROPIC_API_KEY outranks the subscription OAuth token
        # and would silently bill the pay-per-token API instead of the subscription.
        # Refuse to start rather than surprise-bill the user.
        if (deep_on or quick_on) and os.getenv("ANTHROPIC_API_KEY"):
            # 该 key 优先级高于订阅凭据，泄进 Agent SDK 子进程就会悄悄走按 token
            # 计费的 API。客户端已在子进程环境里把它显式置空，所以这里**不再一律
            # 中止**——否则把 anthropic 用作降级 provider 就成了死结：留着 key 启动
            # 被拦，删掉 key 又会在撞额度真要降级时认证失败。
            logger.warning(
                "ANTHROPIC_API_KEY is set while the claude_agent_sdk override is on. "
                "It is stripped from the Agent SDK subprocess so subscription quota is "
                "used, and kept in this process only so an `anthropic` fallback can "
                "still authenticate. If you did not intend to keep a paid Anthropic "
                "fallback, unset it."
            )

        # 降级配置必须成对给：只改 provider 不改 model，会把主 provider 的模型名
        # 配到另一家去（如 AnthropicClient(model="deepseek-chat")），而这条路径
        # **恰好在撞额度、最需要它工作的时候才被走到**——那时再炸就太晚了。
        # 启动时就校验，而不是留到运行中。
        _fb_provider = self.config.get("agent_sdk_fallback_provider")
        _fb_model = self.config.get("agent_sdk_fallback_model")
        if (deep_on or quick_on) and bool(_fb_provider) != bool(_fb_model):
            missing = "agent_sdk_fallback_model" if _fb_provider else "agent_sdk_fallback_provider"
            given = "agent_sdk_fallback_provider" if _fb_provider else "agent_sdk_fallback_model"
            raise ValueError(
                f"{given} is set but {missing} is not — the two must be configured "
                f"together. Otherwise the fallback pairs one provider with another "
                f"provider's model name and fails exactly when the subscription hits "
                f"its quota. Set both, or leave both unset to fall back to "
                f"llm_provider + its own model."
            )

        def _make_client(override_on, sdk_model_key, fallback_model_key):
            """Build a subscription-backed client when overridden, else the normal
            llm_provider client. Fallback rejoins the paid provider on quota/failure."""
            if override_on:
                # backend_url 是为 llm_provider 配的端点。显式指定了**另一家**
                # provider 做降级时不能把它带过去（例如把 anthropic 降级请求发到
                # MiniMax 网关），否则同样是撞额度那一刻才炸。None ⇒ 该 provider
                # 用自己的默认端点。
                cross_provider = bool(_fb_provider) and _fb_provider != self.config["llm_provider"]
                fallback_spec = {
                    "provider": _fb_provider or self.config["llm_provider"],
                    "model": _fb_model or self.config[fallback_model_key],
                    "base_url": None if cross_provider else self.config.get("backend_url"),
                    # 带上 callbacks：降级意味着**开始计费**，此时统计/成本回调
                    # 反而看不到这些调用的话，恰好在花钱的时候统计是瞎的。
                    **({"callbacks": self.callbacks} if self.callbacks else {}),
                    # 用户显式配的输出上限也要带过去。否则撞额度降级之后，降级
                    # provider 用它自己的默认上限，报告照样被截断——而这正是
                    # 用户配 max_tokens 想避免的事（#91）。
                    **({"max_tokens": self.config["max_tokens"]}
                       if self.config.get("max_tokens") else {}),
                }
                return create_llm_client(
                    provider="claude_agent_sdk",
                    model=self.config[sdk_model_key],
                    base_url=self.config.get("backend_url"),
                    fallback_spec=fallback_spec,
                )
            return create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config[fallback_model_key],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )

        deep_client = _make_client(deep_on, "agent_sdk_model", "deep_think_llm")
        quick_client = _make_client(quick_on, "agent_sdk_quick_model", "quick_think_llm")

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()

        # 可选：给单个角色指定模型（#39）。不配 = 完全维持 quick/deep 两档的原行为。
        self.role_llms = self._build_role_llms(
            llm_kwargs, deep_on or quick_on, selected_analysts
        )

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
            max_tool_rounds=self.config["max_tool_rounds_per_analyst"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
            resolve_llm=self.role_llms.get,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100)
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _build_role_llms(
        self,
        llm_kwargs: Dict[str, Any],
        subscription_on: bool,
        selected_analysts=None,
    ) -> Dict[str, Any]:
        """按 `config["role_llms"]` 给单个角色单独建 LLM（#39）。

        默认是空表 —— 不配任何角色时行为与以前完全一致（全部走 quick/deep 两档）。
        配了才有意义：让多空辩手用不同厂商的模型，避免同源模型互相不反驳。

        相同 (provider, model, endpoint) 的角色复用同一个实例，不会因为写了 7 个
        角色就建 7 条连接。
        """
        specs = self.config.get("role_llms") or {}
        if not specs:
            return {}

        unknown = sorted(set(specs) - set(ROLE_KEYS))
        if unknown:
            raise ValueError(
                f"role_llms 里有无法识别的角色名：{unknown}。"
                f"合法角色：{', '.join(ROLE_KEYS)}。"
                f"（写错的角色名如果被静默忽略，你会以为配置生效了，实际没有。）"
            )

        # 没被选中的分析师不会进图，就别为它建模型：那会让一个**永远不执行**的节点
        # 因为缺 API key 或缺可选依赖，把一次本来完全正常的分析在启动时就打断。
        if selected_analysts is not None:
            active = set(selected_analysts)
            skipped = [r for r in specs if r in _ANALYST_ROLES and r not in active]
            if skipped:
                specs = {k: v for k, v in specs.items() if k not in skipped}
                logger.info(
                    "role_llms: 跳过未选中的分析师角色 %s（不建模型）",
                    ", ".join(sorted(skipped)),
                )
            if not specs:
                return {}

        if subscription_on:
            # 订阅覆盖是为了不产生 API 账单，这里显式点名哪些角色会绕开它去计费，
            # 不能让人以为"全部走订阅"却在某几个角色上悄悄花钱。
            logger.warning(
                "role_llms 为以下角色单独指定了模型，它们会绕开 claude_agent_sdk "
                "订阅覆盖、按 token 计费：%s", ", ".join(sorted(specs)),
            )

        main_provider = self.config["llm_provider"]
        cache: Dict[tuple, Any] = {}
        resolved: Dict[str, Any] = {}
        for role, spec in specs.items():
            if not isinstance(spec, dict) or not spec.get("model"):
                raise ValueError(
                    f"role_llms['{role}'] 必须是带 model 的字典，"
                    f'例如 {{"provider": "deepseek", "model": "deepseek-chat"}}。'
                )
            provider = spec.get("provider") or main_provider
            # backend_url 是给主 provider 配的端点。换了厂商还把它带过去，请求就会
            # 发到另一家的网关（和 agent_sdk 降级那里同一个坑）。None = 用该
            # provider 自己的默认端点。
            if "backend_url" in spec:
                base_url = spec["backend_url"]
            elif provider.lower() == str(main_provider).lower():
                base_url = self.config.get("backend_url")
            else:
                base_url = None

            # 主 provider 的**专属**参数不能带给另一家：openai 的
            # `reasoning_effort`、google 的 `thinking_level`、anthropic 的 `effort`
            # 都是各家私有的，塞进 qwen / glm / 自建网关的请求体里可能直接被拒。
            # 通用参数（max_tokens / callbacks 等）保留。
            role_kwargs = (
                llm_kwargs if provider.lower() == str(main_provider).lower()
                else {k: v for k, v in llm_kwargs.items()
                      if k not in _PROVIDER_SPECIFIC_KWARGS}
            )

            key = (provider.lower(), spec["model"], base_url, spec.get("api_key"))
            if key not in cache:
                client_kwargs = dict(role_kwargs)
                if spec.get("api_key"):
                    client_kwargs["api_key"] = spec["api_key"]
                cache[key] = create_llm_client(
                    provider=provider,
                    model=spec["model"],
                    base_url=base_url,
                    **client_kwargs,
                ).get_llm()
            resolved[role] = cache[key]

        logger.info(
            "role_llms: %d 个角色单独配置，实际建了 %d 个模型实例",
            len(resolved), len(cache),
        )
        return resolved

    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        # 与 provider 无关：单次回复的输出上限。撞上它就是报告写一半被截断（#91）。
        max_tokens = self.config.get("max_tokens")
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        return kwargs

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                ]
            ),
            "social": ToolNode(
                [
                    # 情绪分析不只读新闻：资金流是最硬的情绪证据，量价给强度，
                    # 强势股榜给热度归因，新闻负责解释成因（#61）。
                    get_news,
                    get_fund_flow,
                    get_hot_stocks,
                    get_stock_data,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                    get_profit_forecast,
                    get_industry_comparison,
                ]
            ),
            "policy": ToolNode(
                [
                    get_news,
                    get_global_news,
                ]
            ),
            "hot_money": ToolNode(
                [
                    get_stock_data,
                    get_news,
                    get_insider_transactions,
                    get_hot_stocks,
                    get_northbound_flow,
                    get_concept_blocks,
                    get_fund_flow,
                    get_dragon_tiger_board,
                    get_industry_comparison,
                ]
            ),
            "lockup": ToolNode(
                [
                    get_insider_transactions,
                    get_news,
                    get_fundamentals,
                    get_lockup_expiry,
                ]
            ),
        }

    def _fetch_returns(
        self, ticker: str, trade_date: str
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        """Fetch raw and alpha return for ticker measured from trade_date.

        Returns ``(raw_return, alpha_return, actual_holding_days)`` or
        ``(None, None, None)`` when the entry must stay pending.

        两道闸（2026-09 复盘新增，此前两道都没有）：

        1. **基准行日期必须等于 trade_date**。原先直接取 ``iloc[0]`` 当基准价，
           不校验它是不是分析日那一行——停牌日/非交易日会让窗口整体后移，收益与
           标签都指向另一段时间，而且报告里完全看不出来。
        2. **未满最少交易日不结算**。原先只要 ≥2 行就结算：昨天做的决策今天再跑
           同一只票，会拿 1 日收益当持有期回填，反思与绩效口径随之失真。
           ``memory_min_holding_days``（默认 5）满之前一律保持 pending。

        数据源改为与决策同源的 a-stock（个股 + 沪深300 指数），不再用 Yahoo——
        分析用东财/mootdx、结算用 Yahoo 意味着两个口径两套复权，收益不可比。
        """
        from tradingagents.dataflows import a_stock

        holding_days = int(self.config.get("memory_holding_days", 5))
        min_days = int(self.config.get("memory_min_holding_days", 5))
        try:
            start = datetime.strptime(str(trade_date), "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 15)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            stock = a_stock._load_ohlcv_astock(ticker, end_str)
            if stock is None or stock.empty:
                logger.warning(
                    "Could not resolve outcome for %s on %s: no price data (will retry "
                    "next run).", ticker, trade_date,
                )
                return None, None, None
            stock = stock[stock["Date"] >= pd.to_datetime(trade_date)]

            benchmark = a_stock.get_index_daily("000300", trade_date, end_str)
            if benchmark is None or benchmark.empty:
                logger.warning(
                    "Could not resolve outcome for %s on %s: CSI 300 benchmark data "
                    "unavailable (will retry next run).", ticker, trade_date,
                )
                return None, None, None

            # 按日期对齐后再算：两个源各缺几天时，行对行的位置会错开。
            merged = (
                stock[["Date", "Close"]]
                .merge(benchmark[["Date", "Close"]], on="Date", suffixes=("_s", "_b"))
                .sort_values("Date")
                .reset_index(drop=True)
            )
            if merged.empty:
                return None, None, None

            base_date = merged["Date"].iloc[0].strftime("%Y-%m-%d")
            if base_date != str(trade_date):
                logger.warning(
                    "Cannot resolve outcome for %s: %s has no trading row (first available "
                    "is %s). Entry stays pending instead of measuring from the wrong base "
                    "price.", ticker, trade_date, base_date,
                )
                return None, None, None

            elapsed = len(merged) - 1
            if elapsed < min_days:
                logger.info(
                    "%s on %s: only %d trading day(s) elapsed, need %d (memory_min_holding_days)"
                    "; deferring settlement.",
                    ticker, trade_date, elapsed, min_days,
                )
                return None, None, None

            actual_days = min(holding_days, elapsed)
            base_stock = float(merged["Close_s"].iloc[0])
            base_bench = float(merged["Close_b"].iloc[0])
            raw = float(merged["Close_s"].iloc[actual_days]) / base_stock - 1
            bench_ret = float(merged["Close_b"].iloc[actual_days]) / base_bench - 1
            return raw, raw - bench_ret, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s (will retry next run): %s",
                ticker, trade_date, e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(ticker, entry["date"])
            if raw is None:
                continue  # price not available yet — try again next run
            try:
                reflection = self.reflector.reflect_on_final_decision(
                    final_decision=entry.get("decision", ""),
                    raw_return=raw,
                    alpha_return=alpha,
                )
            except Exception as e:
                # 反思是**可选的后处理**，且发生在跑图之前：让一次限流/网络抖动
                # 把整次分析拖挂，是把可选项变成了硬依赖。跳过该条（保持 pending），
                # 下次再结算。
                logger.warning(
                    "Reflection failed for %s on %s (%s: %s); entry stays pending.",
                    ticker, entry.get("date"), type(e).__name__, e,
                )
                continue
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date.

        When ``checkpoint_enabled`` is set in config, the graph is recompiled
        with a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.
        """
        return self._run_graph(company_name, trade_date)

    def _run_fingerprint(self) -> str:
        """本次运行的"配置指纹"，用于断点续跑的 key。

        只按 (ticker, date) 认断点会出两类静默错误：换了模型或改了分析师集合后重跑
        同一天，会静默续用旧状态（已完成阶段用旧模型、剩余阶段用新模型，报告里看不
        出来）；上一次**报错**（不是崩溃）留下的断点也会被当成续跑点。把与结果相关
        的配置一并纳入 key，配置一变就等于开新跑。
        """
        relevant = {
            "llm_provider": self.config.get("llm_provider"),
            "deep_think_llm": self.config.get("deep_think_llm"),
            "quick_think_llm": self.config.get("quick_think_llm"),
            "output_language": self.config.get("output_language"),
            "market_lookback_days": self.config.get("market_lookback_days"),
            "max_debate_rounds": self.config.get("max_debate_rounds"),
            "max_risk_discuss_rounds": self.config.get("max_risk_discuss_rounds"),
            "role_llms": self.config.get("role_llms"),
            "selected_analysts": sorted(self.selected_analysts),
        }
        blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]

    def prepare_graph_run(
        self,
        company_name,
        trade_date,
        callbacks: Optional[List] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[int]]:
        """Prepare graph input/args for a fresh or resumed run.

        Returns ``(initial_state, args, checkpoint_step)``. When a checkpoint
        already exists, ``initial_state`` is ``None`` so LangGraph resumes the
        existing thread instead of replaying completed nodes.
        """
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        checkpoint_enabled = self.config.get("checkpoint_enabled")
        resume_step = None
        fingerprint = self._run_fingerprint() if checkpoint_enabled else ""

        # Recompile with a checkpointer if the user opted in.
        if checkpoint_enabled:
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            resume_step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date), fingerprint
            )
            if resume_step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s",
                    resume_step,
                    company_name,
                    trade_date,
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        args = self.propagator.get_graph_args(callbacks=callbacks)

        # Inject thread_id so same ticker+date(+config) resumes, anything else starts fresh.
        if checkpoint_enabled:
            tid = thread_id(company_name, str(trade_date), fingerprint)
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if checkpoint_enabled and resume_step is not None:
            return None, args, resume_step

        # Initialize state only for fresh runs. Passing a new initial state to
        # LangGraph would start a new run and replay completed nodes.
        past_context = self.memory_log.get_past_context(company_name)
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date, past_context=past_context,
            selected_analysts=self.selected_analysts,
        )
        return init_agent_state, args, resume_step

    def finalize_graph_run(self, company_name, trade_date, final_state):
        """Persist a completed run and clear its checkpoint."""
        self.curr_state = final_state

        # 评级 + 来源：来源必须随决策一起落盘，否则下游只看到一个字符串，
        # 无法区分"模型建议持有"与"评级没解析出来、落到了默认值"。
        decision_text = final_state["final_trade_decision"]
        rating, rating_source = self.signal_processor.process_signal_detail(decision_text)
        final_state["rating_source"] = rating_source
        if rating_source == SOURCE_FALLBACK:
            logger.warning(
                "Rating fell back to %r for %s on %s: no 5-tier rating found in the "
                "final decision (format=%s). Downstream must not treat this as a real Hold.",
                rating, company_name, trade_date,
                final_state.get("final_decision_format", "unknown"),
            )

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=decision_text,
            rating_source=rating_source,
        )

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_fingerprint(),
            )

        return rating

    def close_graph_run(self) -> None:
        """Close the active checkpointer context, if any."""
        if self._checkpointer_ctx is not None:
            self._checkpointer_ctx.__exit__(None, None, None)
            self._checkpointer_ctx = None
            self.graph = self.workflow.compile()

    def _run_graph(self, company_name, trade_date):
        """Execute the graph and write the resulting state to disk and memory log."""
        init_agent_state, args, _ = self.prepare_graph_run(company_name, trade_date)

        try:
            if self.debug:
                trace = []
                for chunk in self.graph.stream(init_agent_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)
                final_state = trace[-1]
            else:
                final_state = self.graph.invoke(init_agent_state, **args)

            signal = self.finalize_graph_run(company_name, trade_date, final_state)
            return final_state, signal
        finally:
            self.close_graph_run()

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        if not self.config.get("persist_state_log", True):
            return
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "policy_report": final_state.get("policy_report", ""),
            "hot_money_report": final_state.get("hot_money_report", ""),
            "lockup_report": final_state.get("lockup_report", ""),
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
