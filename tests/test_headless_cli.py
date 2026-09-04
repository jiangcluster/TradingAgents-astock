"""Headless CLI 单测：`tradingagents-run`（cli.headless）。

只测参数解析 / 合并逻辑 / 失败路径 / 输出 JSON 形状，propagate 用 mock，
不发起任何真实 LLM / 行情请求。
"""
import json
import sys
import types

import pytest

from cli import headless


def _patch_graph(monkeypatch, fake_graph_cls):
    """run_headless 内部做函数级 import，须替换 sys.modules 才能生效。"""
    mod = types.SimpleNamespace(TradingAgentsGraph=fake_graph_cls)
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", mod)


def _full_final_state() -> dict:
    """模拟完整最终状态（含 7 分析师报告、多空/风控辩论、裁决链）。"""
    return {
        "market_report": "市场报告：均线多头排列，量能温和放大。",
        "sentiment_report": "情绪报告：股吧情绪偏中性。",
        "news_report": "新闻报告：近期无重大利空。",
        "fundamentals_report": "基本面报告：营收同比+12%，毛利率稳定。",
        "policy_report": "政策报告：行业政策支持。",
        "hot_money_report": "资金报告：主力资金净流入 2.3 亿。",
        "lockup_report": "",  # 解禁报告为空（无解禁事件）→ 不透传
        "data_quality_summary": "行情数据完整，财务数据覆盖近 4 期",
        "investment_debate_state": {
            "history": "Bull Analyst: 看多理由\nBear Analyst: 看空理由",
            "bull_history": "看多理由",
            "bear_history": "看空理由",
            "judge_decision": "结构化投资计划：买入...",
            "count": 1,
        },
        "trader_investment_plan": "建议分两批买入，目标仓位 5%。",
        "risk_debate_state": {
            "history": "Aggressive Analyst: 积极观点",
            "judge_decision": "综合判断：买入。",
            "count": 1,
        },
        "final_trade_decision": "最终决策：Buy 建议买入",
        "investment_plan": "计划：1/3 仓",
    }


def test_run_headless_output_shape(monkeypatch):
    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            assert selected_analysts == ["market", "news"]
            assert debug is False
            assert config["llm_provider"] == "deepseek"

        def propagate(self, code, date_str):
            assert code == "600519"
            assert date_str == "2026-09-02"
            return (_full_final_state(), "Buy")

    _patch_graph(monkeypatch, FakeGraph)

    result = headless.run_headless(
        "600519", "2026-09-02", ["market", "news"],
        config={"llm_provider": "deepseek"},
    )
    assert result["code"] == "600519"
    assert result["decision"] == "Buy"
    assert "Buy" in result["final_trade_decision"]
    assert result["investment_plan"] == "计划：1/3 仓"

    # analysis_detail：完整分析过程透传
    detail = result["analysis_detail"]
    reports = detail["analyst_reports"]
    # 6 个非空报告透传，为空的解禁报告不透传
    assert set(reports.keys()) == {
        "market", "social", "news", "fundamentals", "policy", "hot_money"}
    assert reports["market"].startswith("市场报告")
    assert detail["data_quality_summary"].startswith("行情数据完整")

    debate = detail["investment_debate"]
    assert debate["history"].startswith("Bull Analyst")
    assert debate["judge_decision"].startswith("结构化投资计划")
    assert debate["rounds"] == 1
    assert detail["trader_investment_plan"].startswith("建议分两批买入")

    risk = detail["risk_debate"]
    assert risk["history"].startswith("Aggressive Analyst")
    assert risk["judge_decision"].startswith("综合判断")
    assert risk["rounds"] == 1
    assert detail["final_trade_decision"] == "最终决策：Buy 建议买入"


def test_run_headless_analysis_detail_missing_fields(monkeypatch):
    """final_state 缺字段时（旧版本引擎/异常状态）不得抛 KeyError，降级为空结构。"""

    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            pass

        def propagate(self, code, date_str):
            return ({"final_trade_decision": "观望"}, "Hold")

    _patch_graph(monkeypatch, FakeGraph)

    result = headless.run_headless(
        "000001", "2026-09-02", ["market"], config={},
    )
    detail = result["analysis_detail"]
    assert detail["analyst_reports"] == {}
    assert detail["data_quality_summary"] == ""
    assert detail["investment_debate"]["history"] == ""
    assert detail["investment_debate"]["rounds"] == 0
    assert detail["risk_debate"]["judge_decision"] == ""
    assert detail["trader_investment_plan"] == ""
    assert detail["final_trade_decision"] == "观望"


def test_main_json_stdout(monkeypatch, capsys):
    monkeypatch.setattr(
        headless, "run_headless",
        lambda code, date_str, analysts, config=None: {
            "code": code, "date": date_str, "decision": "Sell",
            "final_trade_decision": "Sell", "investment_plan": "",
        },
    )
    rc = headless.main(["--code", "600519", "--date", "2026-09-02",
                        "--analysts", "market,news", "--config-json",
                        '{"llm_provider":"deepseek"}'])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["code"] == "600519"
    assert out["decision"] == "Sell"


def test_main_invalid_config_json_exits(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        headless.main(["--code", "600519", "--date", "2026-09-02",
                       "--config-json", "{not json"])
    assert "不是合法 JSON" in str(excinfo.value)


def test_main_failure_returns_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("llm provider down")

    monkeypatch.setattr(headless, "run_headless", boom)
    rc = headless.main(["--code", "600519", "--date", "2026-09-02"])
    assert rc == 1
    assert "深析失败" in capsys.readouterr().err


def test_parse_analysts_rejects_empty():
    with pytest.raises(SystemExit):
        headless._parse_analysts(" , ")


def test_load_config_merges_defaults(monkeypatch):
    fake = types.SimpleNamespace(DEFAULT_CONFIG={"llm_provider": "openai", "a": 1})
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", fake)
    cfg = headless._load_config('{"llm_provider":"deepseek"}')
    assert cfg["llm_provider"] == "deepseek"
    assert cfg["a"] == 1


def test_load_config_headless_is_stateless(monkeypatch):
    """headless 默认不落盘：state log 关、memory 关、results/cache 收敛到 /tmp。"""
    fake = types.SimpleNamespace(DEFAULT_CONFIG={
        "llm_provider": "openai", "persist_state_log": True,
        "memory_log_path": "~/.tradingagents/memory/trading_memory.md",
        "results_dir": "~/.tradingagents/logs",
        "data_cache_dir": "~/.tradingagents/cache",
    })
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", fake)
    cfg = headless._load_config("{}")
    assert cfg["persist_state_log"] is False
    assert cfg["memory_log_path"] == ""
    assert cfg["results_dir"] == headless.tempfile.gettempdir()
    assert cfg["data_cache_dir"] == headless.tempfile.gettempdir()


def test_load_config_explicit_overrides_stateless_defaults(monkeypatch):
    """调用方在 --config-json 显式给开关时，尊重显式值。"""
    fake = types.SimpleNamespace(DEFAULT_CONFIG={
        "llm_provider": "openai", "persist_state_log": True,
        "memory_log_path": "default", "results_dir": "default",
        "data_cache_dir": "default",
    })
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", fake)
    cfg = headless._load_config(
        '{"persist_state_log": true, "memory_log_path": "/x/mem.md",'
        ' "results_dir": "/x/logs", "data_cache_dir": "/x/cache"}'
    )
    assert cfg["persist_state_log"] is True
    assert cfg["memory_log_path"] == "/x/mem.md"
    assert cfg["results_dir"] == "/x/logs"
    assert cfg["data_cache_dir"] == "/x/cache"
