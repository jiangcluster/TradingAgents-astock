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


def test_run_headless_output_shape(monkeypatch):
    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            assert selected_analysts == ["market", "news"]
            assert debug is False
            assert config["llm_provider"] == "deepseek"

        def propagate(self, code, date_str):
            assert code == "600519"
            assert date_str == "2026-09-02"
            return (
                {
                    "final_trade_decision": "最终决策：Buy 建议买入",
                    "investment_plan": "计划：1/3 仓",
                },
                "Buy",
            )

    _patch_graph(monkeypatch, FakeGraph)

    result = headless.run_headless(
        "600519", "2026-09-02", ["market", "news"],
        config={"llm_provider": "deepseek"},
    )
    assert result["code"] == "600519"
    assert result["decision"] == "Buy"
    assert "Buy" in result["final_trade_decision"]


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
