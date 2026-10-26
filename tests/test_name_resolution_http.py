"""中文股票名 → 代码：HTTP 通道优先，mootdx 对照表兜底。

背景（2026-10-26 实测）：TCP 7709 在代理/公司网络里常被整段拦截，而
`resolve_ticker` 只有 mootdx 一条路——于是"把股票名换成代码"这件事要先等完
38 台服务器探测（3~4 分钟），然后报错。名称解析本质是查一张表，HTTP 可用就该
先用 HTTP。

这些用例锁死四条语义：

1. 东财代码检索命中 → 直接返回 6 位代码；
2. 接口可用但无匹配 → **快速**报错，不再白等一轮 TCP 探测；
3. 接口本身不可用 → 回退 mootdx 对照表（向后兼容）；
4. 结果按名称缓存，同一名称不重复请求。
"""

import pytest

from tradingagents.dataflows import a_stock


class _FakeResponse:
    def __init__(self, payload, text=""):
        self._payload = payload
        self.text = text or repr(payload)
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _row(code, name, security_type_name="沪A"):
    return {
        "code": code,
        "shortName": name,
        "securityTypeName": security_type_name,
    }


def _payload(rows):
    return {"code": "0", "msg": "Success", "result": rows}


@pytest.fixture(autouse=True)
def _reset_http_cache(monkeypatch):
    monkeypatch.setattr(a_stock, "_http_name_cache", {})


def test_http_hit_returns_code(monkeypatch):
    rows = [
        _row("600519", "贵州茅台"),
        _row("000858", "五粮液", "深A"),
    ]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResponse(_payload(rows)))

    assert a_stock.resolve_ticker("贵州茅台") == "600519"


def test_http_partial_name_resolves_when_unique(monkeypatch):
    rows = [_row("600519", "贵州茅台")]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResponse(_payload(rows)))

    assert a_stock.resolve_ticker("茅台") == "600519"


def test_http_no_match_fails_fast_without_mootdx(monkeypatch):
    """接口确实回话了但没这只票：必须直接报错，不能再去等 TCP 探测。"""
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResponse(_payload([])))

    called = {"mootdx": False}

    def _fail_if_called():
        called["mootdx"] = True
        raise AssertionError("HTTP 已给出结论，不该再走 mootdx 对照表")

    monkeypatch.setattr(a_stock, "_build_name_code_map", _fail_if_called)

    with pytest.raises(ValueError, match="找不到股票"):
        a_stock.resolve_ticker("不存在的票")
    assert called["mootdx"] is False


def test_http_ambiguous_name_raises(monkeypatch):
    rows = [
        _row("600519", "贵州茅台"),
        _row("600520", "茅台第二"),
    ]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResponse(_payload(rows)))

    with pytest.raises(ValueError, match="匹配到多只股票"):
        a_stock.resolve_ticker("茅台")


def test_http_filters_non_a_share_rows(monkeypatch):
    """港股/基金不能被当成 A 股代码返回。"""
    rows = [
        _row("00700", "腾讯控股", "港股"),
        _row("159915", "创业板ETF", "基金"),
    ]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResponse(_payload(rows)))

    with pytest.raises(ValueError, match="找不到股票"):
        a_stock.resolve_ticker("腾讯控股")


def test_http_failure_falls_back_to_mootdx_map(monkeypatch):
    """接口不可用（网络/解析异常）时保留原有 mootdx 行为。"""

    def _boom(*a, **k):
        raise ConnectionError("eastmoney unreachable")

    monkeypatch.setattr(a_stock, "_em_get", _boom)
    monkeypatch.setattr(
        a_stock,
        "_build_name_code_map",
        lambda: ({"贵州茅台": "600519"}, {"600519": "贵州茅台"}),
    )

    assert a_stock.resolve_ticker("贵州茅台") == "600519"


def test_non_json_response_falls_back_to_mootdx(monkeypatch):
    """接口回 JSONP/HTML 时不能把 JSONDecodeError 当成"找不到股票"抛出去。"""
    monkeypatch.setattr(
        a_stock,
        "_em_get",
        lambda *a, **k: _FakeResponse(ValueError("Expecting value"), text="callback({})"),
    )
    monkeypatch.setattr(
        a_stock,
        "_build_name_code_map",
        lambda: ({"贵州茅台": "600519"}, {"600519": "贵州茅台"}),
    )

    assert a_stock.resolve_ticker("贵州茅台") == "600519"


def test_http_result_is_cached(monkeypatch):
    rows = [_row("600519", "贵州茅台")]
    calls = {"n": 0}

    def _counting_get(*a, **k):
        calls["n"] += 1
        return _FakeResponse(_payload(rows))

    monkeypatch.setattr(a_stock, "_em_get", _counting_get)

    assert a_stock.resolve_ticker("贵州茅台") == "600519"
    assert a_stock.resolve_ticker("贵州茅台") == "600519"
    assert calls["n"] == 1


def test_numeric_ticker_does_not_touch_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("纯数字代码不该发起名称查询")

    monkeypatch.setattr(a_stock, "_em_get", _boom)
    assert a_stock.resolve_ticker("SH600519") == "600519"
