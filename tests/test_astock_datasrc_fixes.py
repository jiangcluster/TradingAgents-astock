"""数据源缺失修复的回归测试（2026-09-05）。

覆盖四处修复：
- BUG1 `_ths_eps_forecast`：同花顺一致预期实际在 id=yjycData 内嵌 JSON，
  页面唯一 HTML 表是「研报评级」图例；旧代码 read_html 回退取 dfs[0]
  → 产出 FY公司评级/EPS=0.0 假数据（且 pandas 3.x 下裸字符串抛
  FileNotFoundError）。现直接从 yjycData 提取，无数据返回空表
- BUG2 `_get_financial_report_sina`：Sina getFinanceReport2022 实际返回
  ``data.report_list`` 为 {报告期: {"data": [科目条目]}} 的 dict，旧代码误取
  ``data[source_type]`` 当 list → 恒解析 0 条（三表全空）
- `_em_get`：push2 / push2his 主站间歇断连 → push2delay 镜像降级
- `get_concept_blocks`：百度股市通 403 → 东财 slist(spt=3) 降级

全部走 monkeypatch，无真实网络。
"""

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import a_stock


class FakeResp:
    """最小 response 替身：json() / text / status_code / encoding。"""

    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# BUG2: Sina 财务三表 report_list 解析
# ---------------------------------------------------------------------------

def _sina_payload():
    """按实测 schema 构造：report_list 是 dict，每期 data 是科目条目数组。

    publish_date 取真实披露节奏：年报次年 4 月、一季报 4 月底、中报 8 月底。
    """
    def period(total, profit, publish_date):
        return {
            "rType": "0",
            "rCurrency": "CNY",
            "publish_date": publish_date,
            "data": [
                {"item_field": "BIZTOTINCO", "item_title": "营业总收入",
                 "item_value": total, "item_source": "lrb"},
                {"item_field": "NETPROFIT", "item_title": "净利润",
                 "item_value": profit, "item_source": "lrb"},
            ],
        }

    return {
        "result": {
            "data": {
                "report_count": 3,
                "report_date": [
                    {"date_value": "20260630", "date_description": "2026年中报", "date_type": 2},
                    {"date_value": "20260331", "date_description": "2026年一季报", "date_type": 1},
                    {"date_value": "20251231", "date_description": "2025年年报", "date_type": 4},
                ],
                "report_list": {
                    "20260630": period("200.00", "20.00", "20260830"),
                    "20260331": period("100.00", "10.00", "20260428"),
                    "20251231": period("500.00", "50.00", "20260420"),
                },
            }
        }
    }


def _patch_sina(monkeypatch, payload=None, exc=None):
    """把 a_stock._requests.get 换成返回固定 payload 的替身。"""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "params": params})
        if exc is not None:
            raise exc
        return FakeResp(payload if payload is not None else _sina_payload())

    monkeypatch.setattr(a_stock._requests, "get", fake_get)
    return calls


def test_sina_report_list_parsed_into_items_by_period(monkeypatch):
    _patch_sina(monkeypatch)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly")

    assert not df.empty, "report_list 应被解析出科目行（旧代码在此返回空表）"
    # 行=科目，列=["科目"] + 各报告期（倒序，YYYY-MM-DD）
    assert list(df.columns) == ["科目", "2026-06-30", "2026-03-31", "2025-12-31"]
    assert list(df["科目"]) == ["营业总收入", "净利润"]
    assert df.iloc[0]["2026-06-30"] == "200.00"
    assert df.iloc[1]["2025-12-31"] == "50.00"


def test_sina_source_type_mapping_per_report_type(monkeypatch):
    calls = _patch_sina(monkeypatch)

    for report_type, expect in (("资产负债表", "fzb"), ("利润表", "lrb"), ("现金流量表", "llb")):
        a_stock._get_financial_report_sina("603613", report_type, "quarterly")

    assert [c["params"]["source"] for c in calls] == ["fzb", "lrb", "llb"]
    assert all(c["params"]["paperCode"] == "sh603613" for c in calls)


def test_sina_sz_prefix_for_non_6_codes(monkeypatch):
    calls = _patch_sina(monkeypatch)

    a_stock._get_financial_report_sina("000001", "利润表", "quarterly")

    assert calls[0]["params"]["paperCode"] == "sz000001"


def test_sina_curr_date_truncates_future_periods(monkeypatch):
    """复盘历史日期时不得把更晚的报告期喂给模型（未来函数）。

    披露日：年报 2026-04-20、一季报 2026-04-28、中报 2026-08-30。
    cutoff=2026-04-25 → 只剩年报，一季报/中报均未披露。
    """
    _patch_sina(monkeypatch)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly", curr_date="2026-04-25")

    assert list(df.columns) == ["科目", "2025-12-31"]
    assert "2026-06-30" not in df.columns
    assert "2026-03-31" not in df.columns


def test_sina_curr_date_filters_by_publish_date_not_period_end(monkeypatch):
    """A4 核心：报告期截止日 <= cutoff 但尚未披露的期必须剔除。

    cutoff=2026-04-25 时一季报（报告期 03-31 <= cutoff）实际 04-28 才披露，
    旧逻辑按报告期截止日判断会把它喂给模型 → 未来函数。
    """
    _patch_sina(monkeypatch)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly", curr_date="2026-04-25")

    assert list(df.columns) == ["科目", "2025-12-31"]
    assert "2026-03-31" not in df.columns

    # 披露日一过（04-28）即放行
    df2 = a_stock._get_financial_report_sina("603613", "利润表", "quarterly", curr_date="2026-05-06")

    assert list(df2.columns) == ["科目", "2026-03-31", "2025-12-31"]


def test_sina_missing_publish_date_falls_back_to_legal_lag(monkeypatch):
    """publish_date 缺失/空时回退法定最晚披露滞后：年报 +4 个月、其余 +3 个月。"""
    payload = _sina_payload()
    rl = payload["result"]["data"]["report_list"]
    rl["20260331"].pop("publish_date")      # 缺失 → 03-31 + 3 月 = 06-30
    rl["20251231"]["publish_date"] = ""     # 空串 → 12-31 + 4 月 = 次年 04-30
    _patch_sina(monkeypatch, payload=payload)

    # cutoff=2026-06-01：一季报回退披露日 06-30 > cutoff → 剔除；年报 04-30 已过 → 保留
    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly", curr_date="2026-06-01")

    assert list(df.columns) == ["科目", "2025-12-31"]

    # cutoff=2026-07-01：一季报回退披露日 06-30 <= cutoff → 放行
    df2 = a_stock._get_financial_report_sina("603613", "利润表", "quarterly", curr_date="2026-07-01")

    assert list(df2.columns) == ["科目", "2026-03-31", "2025-12-31"]


def test_sina_annual_keeps_only_december_periods(monkeypatch):
    _patch_sina(monkeypatch)

    df = a_stock._get_financial_report_sina("603613", "利润表", "annual")

    assert list(df.columns) == ["科目", "2025-12-31"]


def test_sina_caps_periods_at_eight(monkeypatch):
    """原实现 head(8)：最多 8 个报告期，避免返回体无限膨胀。"""
    payload = _sina_payload()
    rl = payload["result"]["data"]["report_list"]
    for i in range(10):
        # 2015~2024 年报，均早于已有期
        rl[f"{2015 + i}1231"] = {
            "data": [{"item_title": "营业总收入", "item_value": str(i)}]
        }
    _patch_sina(monkeypatch, payload=payload)

    df = a_stock._get_financial_report_sina("603613", "利润表", "annual")

    assert len(df.columns) - 1 == 8, f"应裁到 8 个报告期，实际 {len(df.columns) - 1}"


def test_sina_missing_item_in_some_periods_is_none(monkeypatch):
    """各期科目集合不一致时按并集对齐，缺失填 None 而非报错。"""
    payload = _sina_payload()
    payload["result"]["data"]["report_list"]["20260331"]["data"] = [
        {"item_title": "营业总收入", "item_value": "100.00"}
    ]
    _patch_sina(monkeypatch, payload=payload)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly")

    assert df.iloc[0]["2026-03-31"] == "100.00"
    # DataFrame 缺失值被 pandas 存为 NaN（而非 None），用 isna 断言
    assert pd.isna(df.iloc[1]["2026-03-31"])


def test_sina_empty_report_list_returns_empty_df(monkeypatch):
    _patch_sina(monkeypatch, payload={"result": {"data": {"report_count": 0, "report_list": {}}}})

    assert a_stock._get_financial_report_sina("603613", "利润表", "quarterly").empty


def test_sina_legacy_shape_still_returns_empty(monkeypatch):
    """旧 schema（data[source_type] 为 list）不再被误认为有数据。"""
    _patch_sina(monkeypatch, payload={"result": {"data": {"lrb": [{"报告日": "2026-06-30"}]}}})

    assert a_stock._get_financial_report_sina("603613", "利润表", "quarterly").empty


@pytest.mark.parametrize("bad", [{}, {"result": None}, {"result": {"data": None}}])
def test_sina_malformed_payload_returns_empty_df(monkeypatch, bad):
    # 注：None 无法作为畸形用例——_patch_sina 以 payload is None 判定「用默认」，
    # 故此处只覆盖 {} / result=None / data=None 三类真实畸形结构
    _patch_sina(monkeypatch, payload=bad)

    assert a_stock._get_financial_report_sina("603613", "利润表", "quarterly").empty


def test_sina_invalid_period_keys_skipped(monkeypatch):
    payload = _sina_payload()
    payload["result"]["data"]["report_list"]["garbage"] = {"data": [{"item_title": "x", "item_value": "1"}]}
    payload["result"]["data"]["report_list"]["2026"] = {"data": [{"item_title": "y", "item_value": "2"}]}
    _patch_sina(monkeypatch, payload=payload)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly")

    assert list(df.columns) == ["科目", "2026-06-30", "2026-03-31", "2025-12-31"]


def test_sina_connection_reset_is_retried_then_succeeds(monkeypatch):
    """Sina 偶发 10054 重置：重试后应拿到数据，而非直接空表。"""
    attempts = {"n": 0}

    def flaky_get(url, params=None, headers=None, timeout=None, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.ConnectionError("Connection aborted (10054)")
        return FakeResp(_sina_payload())

    monkeypatch.setattr(a_stock._requests, "get", flaky_get)
    monkeypatch.setattr(a_stock.time, "sleep", lambda s: None)

    df = a_stock._get_financial_report_sina("603613", "利润表", "quarterly")

    assert attempts["n"] == 2
    assert not df.empty


def test_sina_persistent_failure_returns_empty_df(monkeypatch):
    """重试耗尽后返回空表（由调用方输出 No data 文案），不抛异常。"""
    attempts = {"n": 0}

    def always_fail(url, params=None, headers=None, timeout=None, **kw):
        attempts["n"] += 1
        raise requests.exceptions.ConnectionError("Connection aborted (10054)")

    monkeypatch.setattr(a_stock._requests, "get", always_fail)
    monkeypatch.setattr(a_stock.time, "sleep", lambda s: None)

    assert a_stock._get_financial_report_sina("603613", "利润表", "quarterly").empty
    assert attempts["n"] == 2, "重试应止于 2 次，不得无限重试"


def test_get_income_statement_emits_csv(monkeypatch):
    _patch_sina(monkeypatch)

    out = a_stock.get_income_statement("603613", freq="quarterly")

    assert out.startswith("# Income Statement for 603613")
    assert "科目,2026-06-30" in out
    assert "营业总收入" in out


def test_get_balance_sheet_reports_no_data_on_empty(monkeypatch):
    _patch_sina(monkeypatch, payload={"result": {"data": {"report_list": {}}}})

    assert a_stock.get_balance_sheet("603613") == (
        "No balance sheet data found for A-stock '603613'"
    )


# ---------------------------------------------------------------------------
# BUG1: 同花顺一致预期（read_html StringIO + yjycData 内嵌 JSON）
# ---------------------------------------------------------------------------

_THS_HTML = """
<html><body>
<table><tr><th>评级</th><th>说明</th></tr><tr><td>公司评级</td><td>12个月内相对沪深300</td></tr></table>
<div id="yjycData" class="none">[["2019","0.91","1.59","SJ"],["2026",null,null,"SJ"],["2027","1.83","13.22","SJ"]]</div>
</body></html>
"""


def test_ths_eps_forecast_parses_yjyc_data(monkeypatch):
    """一致预期取自 id=yjycData 内嵌 JSON，而非（唯一的）评级图例表。"""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: FakeResp(text=_THS_HTML),
    )

    df = a_stock._ths_eps_forecast("603613")

    assert not df.empty
    # null 预测年份被过滤，只留实际值年份
    assert list(df["年度"]) == ["2019", "2027"]
    assert list(df["预测每股收益"]) == [0.91, 1.83]


def test_ths_eps_forecast_no_yjyc_data_returns_empty(monkeypatch):
    """页面无 yjycData（或纯图例）时按"无覆盖"返回空表，不得误取图例表。"""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: FakeResp(text="<html><body><table><tr><th>评级</th><th>说明</th></tr></table></body></html>"),
    )

    assert a_stock._ths_eps_forecast("603613").empty


def test_ths_eps_forecast_malformed_yjyc_data_returns_empty(monkeypatch):
    """yjycData 内容非法 JSON 时返回空表。"""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: FakeResp(text='<div id="yjycData" class="none">not-json</div>'),
    )

    assert a_stock._ths_eps_forecast("603613").empty


def test_ths_eps_forecast_filters_non_eps_rows(monkeypatch):
    """非数字年份 / 非数字 EPS / 缺 EPS 的行应被过滤。"""
    html = (
        '<div id="yjycData" class="none">'
        '[["2026","1.10","5.00","SJ"],["notnum","0.5","1","X"],["2027","abc","1","X"],["2028",null,null,"SJ"]]'
        "</div>"
    )
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: FakeResp(text=html))

    df = a_stock._ths_eps_forecast("603613")

    assert list(df["年度"]) == ["2026"]
    assert list(df["预测每股收益"]) == [1.10]


# ---------------------------------------------------------------------------
# _em_get: push2 / push2his 主站断连 → push2delay 镜像降级
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_em_throttle(monkeypatch):
    """关掉节流 sleep，避免测试被 1s 间隔拖慢。"""
    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)


class FakeSession:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # host -> callable 或 exception
        self.hosts_called = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        self.hosts_called.append(host)
        action = self.behaviour.get(host)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(url)
        return FakeResp({"data": {"ok": host}})


def _patch_session(monkeypatch, behaviour):
    session = FakeSession(behaviour)
    monkeypatch.setattr(a_stock, "_EM_SESSION", session)
    return session


def test_em_get_uses_mirror_when_push2_connection_dropped(monkeypatch):
    """实测：机房到 push2 clist 会 RemoteDisconnected，push2delay 正常。"""
    session = _patch_session(monkeypatch, {
        "push2.eastmoney.com": requests.exceptions.ConnectionError("Remote end closed"),
    })

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get", params={"pn": "1"})

    assert session.hosts_called == ["push2.eastmoney.com", "push2delay.eastmoney.com"]
    assert resp.json()["data"]["ok"] == "push2delay.eastmoney.com"


def test_em_get_uses_mirror_on_timeout(monkeypatch):
    session = _patch_session(monkeypatch, {
        "push2.eastmoney.com": requests.exceptions.Timeout("read timed out"),
    })

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/slist/get")

    assert session.hosts_called[-1] == "push2delay.eastmoney.com"
    assert resp.status_code == 200


def test_em_get_uses_mirror_for_push2his(monkeypatch):
    session = _patch_session(monkeypatch, {
        "push2his.eastmoney.com": requests.exceptions.ConnectionError("reset"),
    })

    a_stock._em_get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get")

    assert session.hosts_called == ["push2his.eastmoney.com", "push2delay.eastmoney.com"]


def test_em_get_uses_mirror_on_5xx(monkeypatch):
    session = _patch_session(monkeypatch, {
        "push2.eastmoney.com": lambda url: FakeResp({}, status_code=503),
    })

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get")

    assert session.hosts_called == ["push2.eastmoney.com", "push2delay.eastmoney.com"]
    assert resp.status_code == 200


def test_em_get_no_mirror_call_when_primary_ok(monkeypatch):
    session = _patch_session(monkeypatch, {})

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get")

    assert session.hosts_called == ["push2.eastmoney.com"]
    assert resp.status_code == 200


def test_em_get_raises_when_both_hosts_fail(monkeypatch):
    """镜像也挂时抛出原异常，由调用方 try/except 输出失败文案。"""
    _patch_session(monkeypatch, {
        "push2.eastmoney.com": requests.exceptions.ConnectionError("down"),
        "push2delay.eastmoney.com": requests.exceptions.ConnectionError("down"),
    })

    with pytest.raises(requests.exceptions.ConnectionError):
        a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get")


def test_em_get_does_not_mirror_non_push2_hosts(monkeypatch):
    """datacenter / search-api 无 push2delay 镜像，失败须原样抛出，不得乱换主机。"""
    session = _patch_session(monkeypatch, {
        "datacenter-web.eastmoney.com": requests.exceptions.ConnectionError("down"),
    })

    with pytest.raises(requests.exceptions.ConnectionError):
        a_stock._em_get("https://datacenter-web.eastmoney.com/api/data/v1/get")

    assert session.hosts_called == ["datacenter-web.eastmoney.com"]


def test_em_get_returns_last_response_when_mirror_also_4xx(monkeypatch):
    session = _patch_session(monkeypatch, {
        "push2.eastmoney.com": lambda url: FakeResp({}, status_code=403),
        "push2delay.eastmoney.com": lambda url: FakeResp({}, status_code=404),
    })

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get")

    assert session.hosts_called == ["push2.eastmoney.com", "push2delay.eastmoney.com"]
    assert resp.status_code == 404


def test_em_get_updates_throttle_timestamp(monkeypatch):
    _patch_session(monkeypatch, {})
    a_stock._em_last_call[0] = 0.0

    a_stock._em_get("https://push2.eastmoney.com/api/qt/clist/get")

    assert a_stock._em_last_call[0] > 0.0


# ---------------------------------------------------------------------------
# get_concept_blocks: 百度 403 → 东财 slist 降级
# ---------------------------------------------------------------------------

_SLIST_DIFF = {
    "0": {"f12": "BK1548", "f14": "综合电商", "f3": 4.68},
    "1": {"f12": "BK0150", "f14": "北京板块", "f3": -0.5},
    "2": {"f12": "BK0634", "f14": "大数据", "f3": 0},
}


def _patch_em_concept(monkeypatch, diff=None, ret=""):
    monkeypatch.setattr(a_stock, "_em_concept_blocks", lambda code: ret)


def test_em_concept_blocks_parses_diff_dict(monkeypatch):
    _patch_session(monkeypatch, {})
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"total": 3, "diff": _SLIST_DIFF}}
    ))

    out = a_stock._em_concept_blocks("603613")

    assert "# Concept & Sector Blocks for 603613" in out
    assert "东方财富 push2" in out
    assert "综合电商: +4.68%" in out
    assert "北京板块: -0.50%" in out
    assert "大数据: +0.00%" in out
    assert "Block tags: 综合电商 / 北京板块 / 大数据" in out


def test_em_concept_blocks_parses_diff_list(monkeypatch):
    """部分东财端点 diff 返回 list 而非 dict，两种形态都要吃下。"""
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"diff": list(_SLIST_DIFF.values())}}
    ))

    out = a_stock._em_concept_blocks("603613")

    assert "综合电商: +4.68%" in out


def test_em_concept_blocks_uses_sz_secid(monkeypatch):
    captured = {}

    def spy(url, params=None, **kw):
        captured["params"] = params
        return FakeResp({"data": {"diff": _SLIST_DIFF}})

    monkeypatch.setattr(a_stock, "_em_get", spy)

    a_stock._em_concept_blocks("000001")

    assert captured["params"]["secid"] == "0.000001"
    assert captured["params"]["spt"] == "3"


def test_em_concept_blocks_empty_returns_blank(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({"data": {"diff": {}}}))

    assert a_stock._em_concept_blocks("603613") == ""


def test_em_concept_blocks_skips_rows_without_name(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"diff": {"0": {"f12": "BK1", "f14": ""}, "1": {"f12": "BK2", "f14": "物联网"}}}}
    ))

    out = a_stock._em_concept_blocks("603613")

    assert "物联网" in out
    assert out.count("\n  ") == 1


def test_em_concept_blocks_non_numeric_change_kept(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"diff": {"0": {"f14": "停牌", "f3": "-"}}}}
    ))

    assert "停牌: -" in a_stock._em_concept_blocks("603613")


def _patch_baidu(monkeypatch, payload=None, exc=None):
    def fake_get(url, headers=None, timeout=None, **kw):
        assert "finance.pae.baidu.com" in url
        if exc is not None:
            raise exc
        return FakeResp(payload)

    monkeypatch.setattr(a_stock, "_requests", type("R", (), {"get": staticmethod(fake_get)}))


def test_concept_blocks_falls_back_when_baidu_403(monkeypatch):
    """百度 403 返回非 JSON → r.json() 抛错 → 走东财降级。"""
    def fake_get(url, headers=None, timeout=None, **kw):
        raise requests.exceptions.JSONDecodeError("Expecting value", "<html>403</html>", 0)

    monkeypatch.setattr(a_stock._requests, "get", fake_get)
    monkeypatch.setattr(a_stock, "_em_concept_blocks", lambda code: "EM_FALLBACK_BODY")

    assert a_stock.get_concept_blocks("603613") == "EM_FALLBACK_BODY"


def test_concept_blocks_falls_back_on_nonzero_resultcode(monkeypatch):
    monkeypatch.setattr(a_stock._requests, "get",
                        lambda *a, **k: FakeResp({"ResultCode": "1", "ResultMsg": "blocked"}))
    monkeypatch.setattr(a_stock, "_em_concept_blocks", lambda code: "EM_FALLBACK_BODY")

    assert a_stock.get_concept_blocks("603613") == "EM_FALLBACK_BODY"


def test_concept_blocks_falls_back_when_baidu_empty(monkeypatch):
    monkeypatch.setattr(a_stock._requests, "get",
                        lambda *a, **k: FakeResp({"ResultCode": "0", "Result": {"603613": []}}))
    monkeypatch.setattr(a_stock, "_em_concept_blocks", lambda code: "EM_FALLBACK_BODY")

    assert a_stock.get_concept_blocks("603613") == "EM_FALLBACK_BODY"


def test_concept_blocks_keeps_baidu_error_when_fallback_empty(monkeypatch):
    """东财也取不到时，保留原错误文案，不能静默变成“无数据”。"""
    monkeypatch.setattr(a_stock._requests, "get",
                        lambda *a, **k: FakeResp({"ResultCode": "1", "ResultMsg": "blocked"}))
    monkeypatch.setattr(a_stock, "_em_concept_blocks", lambda code: "")

    out = a_stock.get_concept_blocks("603613")

    assert "Baidu PAE error" in out and "blocked" in out


def test_concept_blocks_prefers_baidu_when_healthy(monkeypatch):
    """百度正常时不得改动原输出（分类信息比东财扁平列表更全）。"""
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: FakeResp({
        "ResultCode": "0",
        "Result": {"603613": [
            {"name": "行业", "list": [{"name": "互联网服务", "ratio": "+0.7%", "describe": "一级"}]},
            {"name": "概念", "list": [{"name": "物联网", "ratio": "-0.5%", "describe": ""}]},
        ]},
    }))
    called = {"em": False}

    def _em(code):
        called["em"] = True
        return "SHOULD_NOT_BE_USED"

    monkeypatch.setattr(a_stock, "_em_concept_blocks", _em)

    out = a_stock.get_concept_blocks("603613")

    assert called["em"] is False, "百度健康时不应触发降级"
    assert "百度股市通" in out
    assert "## 行业" in out and "互联网服务 (一级): +0.7%" in out
    assert "Concept tags: 物联网" in out


def test_concept_blocks_survives_fallback_exception(monkeypatch):
    """降级本身抛错时要吞掉，仍返回原错误文案而不是新异常。"""
    monkeypatch.setattr(a_stock._requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("baidu down")))

    def boom(code):
        raise RuntimeError("em down")

    monkeypatch.setattr(a_stock, "_em_concept_blocks", boom)

    out = a_stock.get_concept_blocks("603613")

    assert out.startswith("Error fetching concept blocks for 603613")
    assert "baidu down" in out


# ---------------------------------------------------------------------------
# 回归：get_fundamentals 一致预期段（BUG: 用旧 5 列索引解析 2 列 yjycData）
# ---------------------------------------------------------------------------

def _patch_ths_forecast(monkeypatch, rows):
    """mock _ths_eps_forecast，喂 2 列（年度/预测每股收益）DataFrame。"""
    df = pd.DataFrame(rows, columns=["年度", "预测每股收益"])
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: df)
    # Forward PE 段依赖腾讯实时价，mock 成固定价格，专注断言一致预期解析段
    monkeypatch.setattr(
        a_stock, "_tencent_quote",
        lambda codes: {c: {"price": 10.0, "pe_ttm": 5.0} for c in codes},
    )


def test_get_fundamentals_parses_two_column_eps_forecast(monkeypatch):
    """yjycData 修复后是 2 列，get_fundamentals 必须按列名解析。"""
    _patch_ths_forecast(monkeypatch, [("2027", "1.83"), ("2026", "0.91")])

    out = a_stock.get_fundamentals("603613")

    assert "FY2027: EPS=1.83" in out
    assert "FY2026: EPS=0.91" in out
    # 旧 bug 行为：EPS 恒 0 / 误报 low coverage / 无 Forward PE
    assert "EPS=0.0" not in out
    assert "low coverage" not in out
    assert "Forward PE (FY2026):" in out
    assert "PEG:" in out


def test_get_fundamentals_eps_forecast_empty_skips_section(monkeypatch):
    """无一致预期数据时整段略过，不得报错或输出假 EPS。"""
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())

    out = a_stock.get_fundamentals("603613")

    assert "Consensus EPS Forecast" not in out
    assert "FY" not in out
