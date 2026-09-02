"""TradingAgents-astock headless CLI — 单票深析、JSON 输出，供外部自动化系统调用。

与交互式 `tradingagents` CLI / Streamlit Web 的区别：
- 不启动任何 UI/交互面板，一个进程跑完一张票即退出；
- 所有输入走命令行参数，所有结果以一行 JSON 打到 stdout；
- 失败时以非零退出码 + stderr 提示结束，绝不弹交互问卷。

供 a-share-deep-advisor（深研雷达）等管线通过 subprocess 调用。

用法示例：:

    tradingagents-run --code 600362 --date 2026-09-02 \\
        --analysts market,social,news,fundamentals,policy,hot_money,lockup \\
        --config-json '{"llm_provider":"deepseek","deep_think_llm":"deepseek-v4-flash","quick_think_llm":"deepseek-v4-flash","output_language":"Chinese"}'

输出（stdout 单行 JSON）：:

    {"code":"600362","date":"2026-09-02","decision":"Buy",
     "final_trade_decision":"...","investment_plan":"..."}

API key 读取顺序：进程环境变量 → ``load_dotenv()``（cwd/.env）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from typing import List, Optional

from dotenv import load_dotenv


def _parse_analysts(raw: str) -> List[str]:
    """逗号分隔分析师列表 → 校验后的列表。未知角色留待 TA 内部过滤。"""
    items = [a.strip() for a in raw.split(",") if a.strip()]
    if not items:
        raise SystemExit("--analysts 不能为空")
    return items


def _load_config(config_json: str) -> dict:
    """--config-json 覆盖项 → 与 DEFAULT_CONFIG 合并。非法 JSON 直接退出。

    headless 场景默认**不落盘任何产物文件**（避免磁盘被灌满）：
    - ``persist_state_log=False``：不写 full_states_log JSON；
    - ``memory_log_path=""``：完全不写 trading_memory.md（memory.py 空路径即禁用）；
    - ``results_dir`` 收敛到系统临时目录（否则 __init__ 会在 ~/.tradingagents 建目录）。
    调用方在 --config-json 里显式给这三项时，以显式值为准。
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    overrides: dict = {}
    if config_json and config_json.strip():
        try:
            overrides = json.loads(config_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--config-json 不是合法 JSON: {exc}")
        if not isinstance(overrides, dict):
            raise SystemExit("--config-json 必须是 JSON 对象")
    merged = DEFAULT_CONFIG.copy()
    merged.update(overrides)
    # 无状态默认值：仅当调用方未在 --config-json 显式指定时才强制生效
    # （覆盖 DEFAULT_CONFIG，否则 headless 会照默认写盘）。
    if "persist_state_log" not in overrides:
        merged["persist_state_log"] = False
    if "memory_log_path" not in overrides:
        merged["memory_log_path"] = ""
    if "results_dir" not in overrides:
        merged["results_dir"] = tempfile.gettempdir()
    if "data_cache_dir" not in overrides:
        merged["data_cache_dir"] = tempfile.gettempdir()
    return merged


def run_headless(
    code: str,
    date_str: str,
    analysts: List[str],
    config: Optional[dict] = None,
) -> dict:
    """执行单票深析并返回结构化结果（不打印，供 CLI / 单测复用）。"""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
    final_state, decision = graph.propagate(code, date_str)
    return {
        "code": code,
        "date": date_str,
        "decision": decision if isinstance(decision, str) else str(decision),
        "final_trade_decision": str(final_state.get("final_trade_decision", "")),
        "investment_plan": str(final_state.get("investment_plan", "")),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tradingagents-run",
        description="TradingAgents-astock headless 单票深析（JSON 输出，无 UI）。",
    )
    parser.add_argument("--code", required=True, help="6 位股票代码，如 600362")
    parser.add_argument("--date", required=True, help="分析日期 YYYY-MM-DD")
    parser.add_argument(
        "--analysts",
        default="market,social,news,fundamentals,policy,hot_money,lockup",
        help="逗号分隔的分析师角色（默认全 7 个）",
    )
    parser.add_argument(
        "--config-json",
        default="{}",
        help="JSON 配置覆盖项（与 DEFAULT_CONFIG 合并），如 llm_provider/deep_think_llm",
    )
    args = parser.parse_args(argv)

    # API key：环境变量优先；再尝试 cwd/.env（调用方通常 cwd=TA 仓库）。
    load_dotenv()

    try:
        analysts = _parse_analysts(args.analysts)
        config = _load_config(args.config_json)
        result = run_headless(args.code, args.date, analysts, config=config)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：任何失败都要干净退出
        print(f"[tradingagents-run] 深析失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
