"""mootdx 探测时间预算。

网络把 TCP 7709 整段拦截时，端口"能连上"但协议握手全部失败；旧实现会逐台等满
每台自己的建连超时（mootdx 默认 15s），38 台合计 3~4 分钟，首次取数像卡死。
预算用尽必须**立刻**停手：记负缓存、抛错，让调用方走 HTTP 备源（同样的 K 线
0.4s 就能拿到）。

这些用例只锁"预算到点就停"这一条，不改既有语义——"把整张表试完"仍由
`test_mootdx_server_selection.py` 的用例保证（那里服务器是瞬时失败的，预算不会到点）。
"""

import pytest

from tradingagents.dataflows import a_stock


def _fake_env(monkeypatch, servers, factory_calls):
    import mootdx.quotes

    monkeypatch.setattr(a_stock, "_TDX_SERVERS", servers)
    monkeypatch.setattr(a_stock, "_candidate_tdx_servers", lambda: list(servers))
    monkeypatch.setattr(a_stock, "_mootdx_client", None)
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", 0.0)
    monkeypatch.setattr(a_stock, "_probe_tdx", lambda ip, port, timeout=2.0: True)

    class FakeQuotes:
        @staticmethod
        def factory(market="std", server=None, **kwargs):
            factory_calls.append(server)
            raise ConnectionResetError("[Errno 54] Connection reset by peer")

    monkeypatch.setattr(mootdx.quotes, "Quotes", FakeQuotes)


def test_zero_budget_skips_every_verification_call(monkeypatch):
    servers = [("10.0.0.1", 7709), ("10.0.0.2", 7709)]
    calls = []
    _fake_env(monkeypatch, servers, calls)
    monkeypatch.setattr(a_stock, "_MOOTDX_PROBE_BUDGET_S", 0.0)

    with pytest.raises(RuntimeError, match="预算"):
        a_stock._get_mootdx_client()

    assert calls == [], "预算已用尽时不该再建 client 验证"
    assert a_stock._mootdx_unavailable_until > 0, "失败后必须记负缓存，避免每次取数重探"


def test_exhausted_budget_message_mentions_protocol_layer(monkeypatch):
    """报错要能区分"预算用尽"与"连不上"，否则排查方向会被带偏。"""
    calls = []
    _fake_env(monkeypatch, [("10.0.0.1", 7709)], calls)
    monkeypatch.setattr(a_stock, "_MOOTDX_PROBE_BUDGET_S", 0.0)

    with pytest.raises(RuntimeError) as excinfo:
        a_stock._get_mootdx_client()

    message = str(excinfo.value)
    assert "7709" in message
    assert "不再逐台重探" in message


def test_budget_is_checked_before_bare_factory(monkeypatch):
    """预算用尽后不再尝试裸 factory —— 它同样要建连，只会把卡顿延长。"""
    calls = []
    _fake_env(monkeypatch, [("10.0.0.1", 7709)], calls)
    monkeypatch.setattr(a_stock, "_MOOTDX_PROBE_BUDGET_S", 0.0)

    with pytest.raises(RuntimeError):
        a_stock._get_mootdx_client()

    assert all(server is not None for server in calls), (
        f"裸 factory（server=None）不该在预算用尽后调用，实际调用: {calls}"
    )
