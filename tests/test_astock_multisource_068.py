"""多源补齐与「不可得源」标注（0.6.8）单测。

覆盖本次全部新增/变更：
- 新浪个股资金流备源（解析 + 接入 get_fund_flow + 来源披露 + 四档拆分留空）；
- 新浪行业备源（**单位回归**：线上 [5] 已是百分数，**不得再 ×100**）；
- datacenter 三个小节：主要财务指标（含扣非）/ 分红送配 / 股权质押；
- 「不可得数据源纪律」注入 + 门控词表 + 北向无条件停更声明。

设计原则：全部纯函数或 stub 到无网络；三态（正常 / 空 / 异常）+ `None` 必测。
"""
import json

import pytest
import requests

from tradingagents.agents import quality_gate as qg
from tradingagents.agents.utils import agent_utils
from tradingagents.dataflows import a_stock


class _JsonResp:
    """最小 HTTP 响应替身（json() / raise_for_status / content）。"""

    def __init__(self, payload=None, content=None, text=None, status_error=None):
        self._payload = payload
        self.content = content if content is not None else b""
        self.text = text if text is not None else json.dumps(payload or {}, ensure_ascii=False)
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


# ---------------------------------------------------------------------------
# A. 新浪个股资金流（备源）
# ---------------------------------------------------------------------------

def _sina_rows():
    return [
        {"opendate": "2026-09-30", "r0_net": "-22789994.8600", "netamount": "1.0"},
        {"opendate": "2026-09-29", "r0_net": "12345678.0000", "netamount": "-2.0"},
    ]


def test_sina_fund_flow_maps_r0_net_and_blanks_breakdown(monkeypatch):
    """`r0_net`（主力=超大单+大单净额）→ main；四档拆分新浪不提供 → **留空而非 0**。"""
    monkeypatch.setattr(
        a_stock._requests, "get", lambda *a, **k: _JsonResp(_sina_rows()))

    out = a_stock._sina_fund_flow_history("002833")

    assert out["2026-09-30"] == ["2026-09-30", "-22789994.8600", "", "", "", ""]
    assert len(out) == 2
    # 四档必须留空（渲染为 `—`），若写成 "0" 会被下游读成"当日零成交"
    assert out["2026-09-29"][2:] == ["", "", "", ""]


def test_sina_fund_flow_applies_cutoff_and_skips_bad_rows(monkeypatch):
    rows = _sina_rows() + [
        {"opendate": "", "r0_net": "1"},            # 缺日期 → 跳过
        {"opendate": "2026-1", "r0_net": "1"},      # 非法日期 → 跳过
    ]
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _JsonResp(rows))

    out = a_stock._sina_fund_flow_history("002833", cutoff="2026-09-29")

    assert list(out) == ["2026-09-29"]              # 分析日之后的行被截掉
    assert out["2026-09-29"][1] == "12345678.0000"


def test_sina_fund_flow_propagates_http_error(monkeypatch):
    """解析异常/HTTP 错误**不吞**——由 get_fund_flow 降级并留痕。"""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: _JsonResp(status_error=RuntimeError("503")))

    with pytest.raises(RuntimeError):
        a_stock._sina_fund_flow_history("002833")


def test_sina_fund_flow_empty_payload_is_empty_dict(monkeypatch):
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _JsonResp([]))
    assert a_stock._sina_fund_flow_history("002833") == {}


def test_fund_flow_falls_back_to_sina_and_discloses_source(monkeypatch, tmp_path):
    """东财历史为空 → 用新浪补齐，且**必须披露来源**与四档不可得的限制。"""
    monkeypatch.setattr(
        a_stock, "_fund_flow_cache_path", lambda: str(tmp_path / "ff.csv"))
    monkeypatch.setattr(a_stock, "_is_historical", lambda d: False)
    monkeypatch.setattr(a_stock, "_market_today", lambda: __import__("datetime").date(2026, 9, 30))
    # 东财重启/历史都返回空（真实形态：push2 与镜像同时断连）
    monkeypatch.setattr(
        a_stock, "_em_get", lambda *a, **k: _JsonResp({"data": {"klines": []}}))
    monkeypatch.setattr(a_stock, "_sina_fund_flow_history",
                        lambda *a, **k: {"2026-09-30": ["2026-09-30", "-22789994.86", "", "", "", ""]})

    text = a_stock.get_fund_flow("002833", "2026-09-30")

    assert "数据来源：新浪 MoneyFlow 1 天 + 本地累积 0 天" in text
    assert "| main=-2279" in text                    # 主力净额（元 → 万元）
    assert "新浪不提供" in text                       # 四档限制必须写明
    assert "不得当作 0" in text


# ---------------------------------------------------------------------------
# B. 新浪行业（备源）
# ---------------------------------------------------------------------------

def _hy_resp(pairs):
    body = "var S_Finance_bankuai_sinaindustry = " + json.dumps(pairs, ensure_ascii=False)
    return _JsonResp(payload=None, content=body.encode("gbk"))


def test_sina_industry_uses_pct_as_percent_not_ratio(monkeypatch):
    """**单位回归**：线上 [5] 已是百分数 → 输出必须等于原值，不得再 ×100。

    首次实现误当成"比例"再乘 100，实测跑出 `-166.70%` 这种不可能的日涨跌幅。
    """
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _hy_resp({
        "new_blhy": "new_blhy,玻璃行业,19,16.733,-1.667,-1.667,410050588,9066360698,"
                    "sz300395,2.395,94.920,2.220,菲利华",
    }))

    rows = a_stock._sina_industry_summary()

    assert len(rows) == 1
    assert rows[0]["name"] == "玻璃行业"             # GBK 正确解码
    assert rows[0]["pct"] == pytest.approx(-1.667)   # **不得**是 -166.7
    assert rows[0]["amount_yi"] == "90.7"            # 元 → 亿元
    assert rows[0]["leader"] == "菲利华"


def test_sina_industry_skips_short_rows_and_sorts_desc(monkeypatch):
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _hy_resp({
        "a": "a,行业A,10,1,0.1,1.0,1,1,sz1,1,1,1,甲",
        "b": "b,行业B,10,1,0.1,3.0,1,1,sz1,1,1,1,乙",
        "bad": "too,short",
    }))

    rows = a_stock._sina_industry_summary()

    assert [r["name"] for r in rows] == ["行业B", "行业A"]   # 降序
    assert len(rows) == 2                                     # 短行被跳过


def test_sina_industry_raises_without_json_object(monkeypatch):
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: _JsonResp(payload=None, content=b"<html>404</html>"))
    with pytest.raises(ValueError):
        a_stock._sina_industry_summary()


def test_render_sina_industry_marks_classification_and_both_ends(monkeypatch):
    monkeypatch.setattr(a_stock, "_sina_industry_summary", lambda: [
        {"name": f"行业{i}", "pct": float(10 - i), "count": "5",
         "amount_yi": "1.0", "leader": "某股"} for i in range(6)
    ])
    lines = []

    assert a_stock._render_sina_industry(lines, 2, "东财被拦") is True
    body = "\n".join(lines)

    assert "备源：新浪行业" in body and "行业指数级" in body   # 分类体系与口径必须标注
    assert "涨1. 行业0" in body and "跌2. 行业5" in body       # 两头都给


def test_render_sina_industry_returns_false_on_failure(monkeypatch):
    """备源本身失败 → 返回 False，由调用方回落到「取数失败/为空」文案（不得静默）。"""
    def boom():
        raise RuntimeError("sina down")

    monkeypatch.setattr(a_stock, "_sina_industry_summary", boom)
    assert a_stock._render_sina_industry([], 2, "x") is False


# ---------------------------------------------------------------------------
# C. datacenter 小节
# ---------------------------------------------------------------------------

def test_main_financial_section_renders_kcfjcxsyjlr(monkeypatch):
    """扣非列必须在（此前长期标注 `[数据缺失: 官方扣非净利润科目]`）；金额换算为亿元。"""
    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [{
        "REPORT_DATE_NAME": "2026中报",
        "TOTALOPERATEREVE": 1503324526.04, "TOTALOPERATEREVETZ": 21.7489,
        "PARENTNETPROFIT": 254725667.97, "PARENTNETPROFITTZ": 5.0887,
        "KCFJCXSYJLR": 233501089.47, "KCFJCXSYJLRTZ": 14.3965,
        "ROEJQ": 8.52, "ROEKCJQ": 7.81, "XSMLL": 33.0222, "XSJLL": 17.1702,
        "EPSJB": 0.6, "ZCFZL": 29.6780,
    }])

    body = "\n".join(a_stock._datacenter_main_financial_section("002833"))

    assert "扣非归母净利(亿元) | 2.34" in body
    assert "营业总收入(亿元) | 15.03" in body
    assert "ROE加权(%) | 8.52" in body
    assert "法定披露口径" in body


def test_main_financial_section_empty_and_none_values(monkeypatch):
    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [])
    assert a_stock._datacenter_main_financial_section("002833") == []

    monkeypatch.setattr(a_stock, "_eastmoney_datacenter",
                        lambda *a, **k: [{"REPORT_DATE_NAME": "2026中报", "ROEJQ": None}])
    body = "\n".join(a_stock._datacenter_main_financial_section("002833"))
    assert "ROE加权(%) | —" in body          # None → `—`，不得渲染成 0


def test_dividend_section_scales_ratio_to_percent(monkeypatch):
    """`DIVIDENT_RATIO` 线上是**比例**（0.0158646 → 1.59%），必须 ×100。"""
    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [{
        "REPORT_DATE": "2026-06-30 00:00:00", "PRETAX_BONUS_RMB": 3,
        "DIVIDENT_RATIO": 0.015864621893, "EX_DIVIDEND_DATE": "2026-09-09 00:00:00",
        "EQUITY_RECORD_DATE": "2026-09-08 00:00:00", "ASSIGN_PROGRESS": "实施分配",
        "IMPL_PLAN_PROFILE": "10派3.00元(含税,扣税后2.70元)",
    }])

    body = "\n".join(a_stock._datacenter_dividend_section("002833"))

    assert "10派3.00元(含税,扣税后2.70元)" in body
    assert "| 1.59 " in body                 # 比例 → 百分数
    assert "每10股派息(元,含税)" in body


def test_pledge_section_empty_returns_empty_list(monkeypatch):
    """无记录必须返回 `[]`（恒非空会让 mootdx F10 回退变死代码）。"""
    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [])
    assert a_stock._datacenter_pledge_section("002833") == []


def test_pledge_section_omits_unverified_amount_fields(monkeypatch):
    """金额/股数类字段单位未线上核实 → 不输出（避免下游误读为亿元/万股）。"""
    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [{
        "TRADE_DATE": "2021-05-21 00:00:00", "PLEDGE_RATIO": 2.05,
        "PLEDGE_DEAL_NUM": 1, "PLEDGE_MARKET_CAP": 21613.7988,
        "REPURCHASE_BALANCE": 444.18,
    }])

    body = "\n".join(a_stock._datacenter_pledge_section("002833"))

    assert "2.05" in body and "2021-05-21" in body
    assert "21613" not in body and "444.18" not in body
    assert "单位未经线上核实" in body


# ---------------------------------------------------------------------------
# D. 不可得源标注
# ---------------------------------------------------------------------------

def test_language_instruction_carries_unavailable_source_rule():
    rule = agent_utils.get_language_instruction()
    assert "不可得数据源纪律" in rule
    assert "北向资金" in rule and "政策文件原文" in rule and "海外收入占比" in rule
    assert "不得**用你自己的知识" in rule
    assert "既不作利多、也不作利空" in rule


def test_failure_markers_include_stale_source_marker():
    assert a_stock._NB_STALE_MARKER == "[数据源停更]"
    assert a_stock._NB_STALE_MARKER in qg.FAILURE_MARKERS


def test_northbound_output_always_declares_permanently_unavailable(monkeypatch):
    """取数**失败**时也必须带停更声明——否则下游会读成"暂时缺数据、等恢复"。"""
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hexin down")))

    out = a_stock.get_northbound_flow("2026-09-30")

    assert a_stock._NB_STALE_MARKER in out
    assert "数据源已永久不可得" in out
    assert "不得作为任何方向的" in out
