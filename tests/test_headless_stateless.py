"""headless 无状态改造单测：persist_state_log=False 时不落盘产物文件。

只测写盘开关，不发起任何真实 LLM / 行情请求。
"""
import json
from unittest.mock import MagicMock

from tradingagents.graph.trading_graph import TradingAgentsGraph


def _final_state():
    return {
        "company_of_interest": "600519",
        "trade_date": "2026-09-02",
        "market_report": "x",
        "sentiment_report": "x",
        "news_report": "x",
        "fundamentals_report": "x",
        "investment_debate_state": {
            "bull_history": "", "bear_history": "", "history": "",
            "current_response": "", "judge_decision": "",
        },
        "investment_plan": "",
        "trader_investment_plan": "",
        "risk_debate_state": {
            "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "history": "", "judge_decision": "",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "count": 1, "latest_speaker": "",
        },
        "final_trade_decision": "Buy",
    }


def test_log_state_skipped_when_persist_disabled(tmp_path):
    """persist_state_log=False：不创建目录、不写 full_states_log JSON。"""
    graph = MagicMock()
    graph.config = {"results_dir": str(tmp_path), "persist_state_log": False}
    graph.log_states_dict = {}
    graph.ticker = "600519"
    TradingAgentsGraph._log_state(graph, "2026-09-02", _final_state())
    assert not (tmp_path / "600519").exists(), "不应创建 ticker 目录"


def test_log_state_writes_when_persist_enabled(tmp_path):
    """默认（persist_state_log=True）：维持原行为，写出 full_states_log JSON。"""
    graph = MagicMock()
    graph.config = {"results_dir": str(tmp_path), "persist_state_log": True}
    graph.log_states_dict = {}
    graph.ticker = "600519"
    TradingAgentsGraph._log_state(graph, "2026-09-02", _final_state())
    log = tmp_path / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-09-02.json"
    assert log.is_file()
    data = json.loads(log.read_text(encoding="utf-8"))
    assert data["trade_date"] == "2026-09-02"
