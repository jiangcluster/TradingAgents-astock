"""P0/P1 数据采集与计算缺陷修复的回归测试（2026-09-06）。

覆盖六处修复（全部 monkeypatch，无真实网络）：
- A1 `_em_kline_fallback`：东财 kline 行格式为 日期/开/收/高/低/量（收盘在 p[2]），
  旧代码按顺序 OHLC 解析 → High/Low/Close 三列错位，MA/振幅/涨跌幅全部失真
- A2+A3 `_tencent_quote`：vals[44]/vals[45] 市值字段互换（实测工商银行
  44=21919.47 < 45=28975.83，交叉核对东财 f117/f116 确认 44=流通、45=总市值）；
  vals[52] 实为「市盈率(动)」，旧代码标为 PE (Static) 误导模型
- A5 `get_northbound_flow`：同花顺 dayChart 的 sgt 盘中约 35 点后停更且量级异口径，
  旧代码直接 hgt[-1]+sgt[-1] → -9.28+379.75=+370.47 亿假净流入信号
- A6 `get_industry_comparison`：clist 缺 fid → 服务端按 f12(代码) 排序，
  「涨跌幅排名前 N」与涨跌幅无关；f140 是领涨股代码而非名称；缺 f106(平盘家数)
- A7 `_tencent_kline_fallback`：降级链原为 mootdx→东财→新浪，缺无节流的中间源
"""

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import a_stock


class FakeResp:
    """最小 response 替身：json() / text / status_code / raise_for_status()。"""

    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


# ---------------------------------------------------------------------------
# A1: 东财 kline 列序（收盘在 p[2]，非顺序 OHLC）
# ---------------------------------------------------------------------------

_EM_KLINE_ROW = "2026-09-04,10.50,10.80,10.95,10.40,123456,1.33e9,0.0279,2.86,10.80,10.50"


def test_em_kline_fallback_maps_close_high_low_correctly(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"klines": [_EM_KLINE_ROW]}}
    ))

    df = a_stock._em_kline_fallback("600519")

    row = df.iloc[0]
    assert row["Open"] == 10.50
    assert row["Close"] == 10.80, "东财 p[2] 是收盘价，旧代码误当最高价"
    assert row["High"] == 10.95
    assert row["Low"] == 10.40
    assert row["Volume"] == 123456 * 100, "成交量单位「手」，须 ×100 换算为股"


def test_em_kline_fallback_ohlc_invariant_holds(monkeypatch):
    """列序错位时 Low>Close 或 High<Close 必然出现；修复后恒满足 Low<=Open/Close<=High。"""
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"klines": [_EM_KLINE_ROW]}}
    ))

    row = a_stock._em_kline_fallback("600519").iloc[0]

    assert row["Low"] <= row["Open"] <= row["High"]
    assert row["Low"] <= row["Close"] <= row["High"]


def test_em_kline_fallback_skips_short_rows(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(
        {"data": {"klines": ["2026-09-04,10.5,10.8", _EM_KLINE_ROW]}}
    ))

    df = a_stock._em_kline_fallback("600519")

    assert len(df) == 1


def test_em_kline_fallback_empty_returns_empty_df(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({"data": {}}))

    assert a_stock._em_kline_fallback("600519").empty


# ---------------------------------------------------------------------------
# A7: 腾讯 kline 降级源
# ---------------------------------------------------------------------------

def _tencent_kline_payload(prefixed="sh600519", key="qfqday"):
    rows = [
        # [date, open, close, high, low, volume(手)]
        ["2026-09-01", "1400.0", "1420.0", "1435.0", "1390.0", "25000"],
        ["2026-09-02", "1420.0", "1410.0", "1440.0", "1405.0", "30000"],
        ["2026-09-03", "1410.0", "1450.0", "1460.0", "1408.0", "28000"],
    ]
    return {"data": {prefixed: {key: rows}}}


def _patch_tencent_kline(monkeypatch, payload):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url})
        return FakeResp(text=__import__("json").dumps(payload))

    monkeypatch.setattr(a_stock._requests, "get", fake_get)
    return calls


def test_tencent_kline_fallback_maps_columns(monkeypatch):
    _patch_tencent_kline(monkeypatch, _tencent_kline_payload())

    df = a_stock._tencent_kline_fallback("600519")

    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    row = df.iloc[0]
    assert row["Open"] == 1400.0
    assert row["Close"] == 1420.0, "腾讯 idx2 是收盘价"
    assert row["High"] == 1435.0
    assert row["Low"] == 1390.0
    assert row["Volume"] == 25000 * 100
    assert len(df) == 3


def test_tencent_kline_fallback_url_uses_prefix_and_qfq(monkeypatch):
    calls = _patch_tencent_kline(monkeypatch, _tencent_kline_payload())

    a_stock._tencent_kline_fallback("600519")

    url = calls[0]["url"]
    assert "param=sh600519,day,,,800,qfq" in url
    assert "web.ifzq.gtimg.cn/appstock/app/fqkline/get" in url


def test_tencent_kline_fallback_bj_prefix_for_920_codes(monkeypatch):
    """北交所 920xxx 须走 bj 前缀（_get_prefix 的 92 特判），否则取到空 payload。"""
    calls = _patch_tencent_kline(monkeypatch, _tencent_kline_payload(prefixed="bj920001"))

    df = a_stock._tencent_kline_fallback("920001")

    assert "param=bj920001" in calls[0]["url"]
    assert len(df) == 3


def test_tencent_kline_fallback_uses_day_key_when_qfqday_missing(monkeypatch):
    _patch_tencent_kline(monkeypatch, _tencent_kline_payload(key="day"))

    df = a_stock._tencent_kline_fallback("600519")

    assert len(df) == 3


def test_tencent_kline_fallback_filters_by_date_range(monkeypatch):
    _patch_tencent_kline(monkeypatch, _tencent_kline_payload())

    df = a_stock._tencent_kline_fallback("600519", start_date="2026-09-02", end_date="2026-09-02")

    assert len(df) == 1
    assert df.iloc[0]["Date"] == pd.Timestamp("2026-09-02")


def test_tencent_kline_fallback_empty_returns_empty_df(monkeypatch):
    _patch_tencent_kline(monkeypatch, {"data": {}})

    assert a_stock._tencent_kline_fallback("600519").empty


def test_tencent_kline_fallback_skips_malformed_rows(monkeypatch):
    payload = _tencent_kline_payload()
    payload["data"]["sh600519"]["qfqday"].insert(0, ["2026-08-31", "bad"])
    _patch_tencent_kline(monkeypatch, payload)

    df = a_stock._tencent_kline_fallback("600519")

    assert len(df) == 3


def _patch_ohlcv_chain(monkeypatch, tmp_path, *, em_df, tencent_df, tencent_raises=False):
    """把 _load_ohlcv_astock 的上游全部替换为可控替身（mootdx 恒失败）。"""
    from tradingagents.dataflows import config as ta_config

    calls = []
    monkeypatch.setattr(ta_config, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(a_stock, "_mootdx_call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mootdx down")))
    monkeypatch.setattr(a_stock, "_em_kline_fallback", lambda *a, **k: em_df)
    if tencent_raises:
        monkeypatch.setattr(a_stock, "_tencent_kline_fallback",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tencent down")))
    else:
        def fake_tencent(*a, **k):
            calls.append("tencent")
            return tencent_df
        monkeypatch.setattr(a_stock, "_tencent_kline_fallback", fake_tencent)
    monkeypatch.setattr(a_stock, "_sina_kline_fallback",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sina down")))
    monkeypatch.setattr(a_stock, "_supplement_stale_ohlcv_with_sina",
                        lambda code, df, curr_date, start_date=None: (df, False))
    return calls


def _one_row_df():
    return pd.DataFrame({
        "Date": [pd.Timestamp("2026-09-03")],
        "Open": [1410.0], "High": [1460.0],
        "Low": [1408.0], "Close": [1450.0], "Volume": [2800000.0],
    })


def test_ohlcv_chain_uses_tencent_when_eastmoney_empty(monkeypatch, tmp_path):
    """A7：降级链须为 mootdx→东财→腾讯→新浪，东财空时腾讯被调用且不再降级到新浪。"""
    calls = _patch_ohlcv_chain(monkeypatch, tmp_path,
                               em_df=pd.DataFrame(), tencent_df=_one_row_df())

    df = a_stock._load_ohlcv_astock("600519", "2026-09-04")

    assert calls == ["tencent"]
    assert not df.empty
    assert float(df.iloc[0]["Close"]) == 1450.0


def test_ohlcv_chain_skips_tencent_when_eastmoney_ok(monkeypatch, tmp_path):
    calls = _patch_ohlcv_chain(monkeypatch, tmp_path,
                               em_df=_one_row_df(), tencent_df=_one_row_df())

    df = a_stock._load_ohlcv_astock("600519", "2026-09-04")

    assert calls == [], "东财已返回数据，不应再请求腾讯"
    assert not df.empty


def test_ohlcv_chain_error_message_names_all_four_sources(monkeypatch, tmp_path):
    _patch_ohlcv_chain(monkeypatch, tmp_path, em_df=pd.DataFrame(),
                       tencent_df=None, tencent_raises=True)

    with pytest.raises(ValueError) as exc:
        a_stock._load_ohlcv_astock("600519", "2026-09-04")

    assert "mootdx/eastmoney/tencent/sina" in str(exc.value)


# ---------------------------------------------------------------------------
# A2+A3: 腾讯实时行情市值字段与 PE 口径
# ---------------------------------------------------------------------------

def _tencent_quote_raw(vals_map=None, code="601398", name="工商银行"):
    """构造 qt.gtimg.cn 返回体（GBK），vals 至少 53 段。"""
    vals = [""] * 60
    vals[0] = "1"
    vals[1] = name
    vals[2] = code
    for idx, v in (vals_map or {}).items():
        vals[idx] = v
    return f'v_sh{code}="{"~".join(vals)}";\n'


def _patch_tencent_quote(monkeypatch, raw):
    class FakeUrlopen:
        def __init__(self, *a, **k):
            pass

        def read(self):
            return raw.encode("gbk")

    monkeypatch.setattr(a_stock.urllib.request, "urlopen", lambda *a, **k: FakeUrlopen())


def test_tencent_quote_mcap_fields_not_swapped(monkeypatch):
    """A2：实测工商银行 vals[44]=21919.47(流通) < vals[45]=28975.83(总市值)。"""
    _patch_tencent_quote(monkeypatch, _tencent_quote_raw({
        3: "7.25", 4: "7.20", 5: "7.21", 32: "0.69", 33: "7.28", 34: "7.19",
        38: "0.31", 39: "6.80",
        44: "21919.47", 45: "28975.83", 46: "0.68",
        47: "7.92", 48: "6.48", 52: "6.55",
    }))

    q = a_stock._tencent_quote(["601398"])["601398"]

    assert q["float_mcap_yi"] == 21919.47
    assert q["mcap_yi"] == 28975.83
    assert q["mcap_yi"] > q["float_mcap_yi"], "总市值不得小于流通市值"


def test_tencent_quote_pe_dynamic_key_not_static(monkeypatch):
    """A3：vals[52] 是「市盈率(动)」，键名须为 pe_dynamic（旧代码标 pe_static）。"""
    _patch_tencent_quote(monkeypatch, _tencent_quote_raw({3: "7.25", 39: "6.80", 52: "6.55"}))

    q = a_stock._tencent_quote(["601398"])["601398"]

    assert q["pe_dynamic"] == 6.55
    assert "pe_static" not in q


def test_tencent_quote_short_payload_skipped(monkeypatch):
    _patch_tencent_quote(monkeypatch, 'v_sh601398="1~工商银行~601398~7.25";\n')

    assert a_stock._tencent_quote(["601398"]) == {}


def test_tencent_quote_empty_fields_default_zero(monkeypatch):
    _patch_tencent_quote(monkeypatch, _tencent_quote_raw({3: "7.25"}))

    q = a_stock._tencent_quote(["601398"])["601398"]

    assert q["price"] == 7.25
    assert q["mcap_yi"] == 0
    assert q["pe_dynamic"] == 0


# ---------------------------------------------------------------------------
# A5: 北向 sgt 盘中停更 → 不得混入 Total
# ---------------------------------------------------------------------------

def _hsgt_payload(n_times=262, n_sgt=35, hgt_last=-9.28, sgt_last=379.75):
    times = [f"09:{30 + i // 60:02d}" for i in range(n_times)]
    hgt = [0.0] * (n_times - 1) + [hgt_last]
    sgt = [0.0] * (n_sgt - 1) + [sgt_last]
    return {"time": times, "hgt": hgt, "sgt": sgt}


def _patch_hsgt(monkeypatch, tmp_path, payload):
    path = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(payload))
    return path


def test_northbound_truncated_sgt_not_added_to_total(monkeypatch, tmp_path):
    """sgt 仅 35 点而 time 有 262 点 → 视为不可用，Total 仅计 HGT。"""
    _patch_hsgt(monkeypatch, tmp_path, _hsgt_payload(n_times=262, n_sgt=35))

    out = a_stock.get_northbound_flow("2026-09-06")

    assert "SGT(深股通)=N/A(上游盘中停更)" in out
    assert "Total(仅HGT)=-9.28亿" in out
    assert "370.47" not in out, "不得再出现 -9.28+379.75 的假净流入合计"
    assert "OUTFLOW" in out, "沪股通净流出须给出 bearish 信号"


def test_northbound_full_sgt_included_in_total(monkeypatch, tmp_path):
    """sgt 覆盖完整时间序列时照常合计。"""
    _patch_hsgt(monkeypatch, tmp_path,
                _hsgt_payload(n_times=10, n_sgt=10, hgt_last=-9.28, sgt_last=5.00))

    out = a_stock.get_northbound_flow("2026-09-06")

    assert "SGT(深股通)=5.00亿" in out
    assert "Total=-4.28亿" in out


def test_northbound_truncated_sgt_cached_as_nan(monkeypatch, tmp_path):
    """缓存须写 nan 占位而非 0，避免把「缺数据」当「净流入 0 亿」污染历史均值。"""
    path = _patch_hsgt(monkeypatch, tmp_path, _hsgt_payload(n_times=262, n_sgt=35))

    a_stock.get_northbound_flow("2026-09-06")

    text = path.read_text(encoding="utf-8")
    assert text.strip().splitlines()[-1].endswith(",nan")


def test_northbound_history_nan_row_shown_as_na(monkeypatch, tmp_path):
    path = _patch_hsgt(monkeypatch, tmp_path, _hsgt_payload(n_times=10, n_sgt=10,
                                                            hgt_last=1.0, sgt_last=2.0))
    path.write_text(
        "date,hgt,sgt\n2026-09-01,10.00,-4.00\n2026-09-02,-9.28,nan\n",
        encoding="utf-8",
    )

    out = a_stock.get_northbound_flow("2026-09-06", include_history=True)

    assert "2026-09-02: HGT=-9.28 SGT=N/A Total=-9.28" in out
    # 均值口径：nan 行仅按 HGT 计 → (6.0 + (-9.28) + 3.0) / 3 = -0.09
    assert "-0.09亿" in out


def test_northbound_empty_realtime_no_crash(monkeypatch, tmp_path):
    _patch_hsgt(monkeypatch, tmp_path, {"time": [], "hgt": [], "sgt": []})

    out = a_stock.get_northbound_flow("2026-09-06")

    assert "No realtime data" in out


# ---------------------------------------------------------------------------
# A6: clist 排序字段 fid / 领涨股 f128 / 平盘家数 f106
# ---------------------------------------------------------------------------

_CLIST_ITEM = {
    "f2": 1234.0, "f3": 3.21, "f4": 12.0, "f12": "BK0475", "f13": 90,
    "f14": "银行", "f104": 30, "f105": 8, "f106": 4,
    "f128": "招商银行", "f136": 302000, "f140": "600036", "f141": "sh600036",
    "f207": "银行",
}


def _patch_clist(monkeypatch, items):
    calls = []

    def spy(url, params=None, timeout=None, **kw):
        calls.append({"url": url, "params": params})
        return FakeResp({"data": {"diff": items}})

    monkeypatch.setattr(a_stock, "_em_get", spy)
    return calls


def test_industry_comparison_requests_fid_f3(monkeypatch):
    """缺 fid 时东财按 f12(代码) 排序，「排名前 N」与涨跌幅无关。"""
    calls = _patch_clist(monkeypatch, [_CLIST_ITEM])

    a_stock.get_industry_comparison("600036", "2026-09-04")

    params = calls[0]["params"]
    assert params["fid"] == "f3"
    assert params["po"] == "1"


def test_industry_comparison_requests_flat_and_leader_fields(monkeypatch):
    calls = _patch_clist(monkeypatch, [_CLIST_ITEM])

    a_stock.get_industry_comparison("600036", "2026-09-04")

    fields = calls[0]["params"]["fields"].split(",")
    assert "f106" in fields, "缺 f106(平盘家数) → 涨/跌/平三家数不自洽"
    assert "f128" in fields, "f128 才是领涨股名称"


def test_industry_comparison_shows_flat_count_and_leader_name(monkeypatch):
    _patch_clist(monkeypatch, [_CLIST_ITEM])

    out = a_stock.get_industry_comparison("600036", "2026-09-04")

    assert "平盘" in out
    assert "1. 银行 | 3.21% | 30 | 8 | 4 | 招商银行" in out
    # 领涨股列须为名称（f128）而非代码（f140=600036）
    data_line = [ln for ln in out.splitlines() if ln.strip().startswith("1.")][0]
    assert "招商银行" in data_line
    assert "600036" not in data_line


def test_industry_comparison_leader_falls_back_to_code(monkeypatch):
    item = dict(_CLIST_ITEM)
    item.pop("f128")
    _patch_clist(monkeypatch, [item])

    out = a_stock.get_industry_comparison("600036", "2026-09-04")

    # f128 缺失 → 回退领涨股代码 f140
    assert "600036" in out


def test_industry_comparison_empty_items(monkeypatch):
    _patch_clist(monkeypatch, [])

    out = a_stock.get_industry_comparison("600036", "2026-09-04")

    assert "行业数据获取为空" in out


def test_industry_comparison_error_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("clist down")

    monkeypatch.setattr(a_stock, "_em_get", boom)

    out = a_stock.get_industry_comparison("600036", "2026-09-04")

    assert "行业对比查询失败" in out
