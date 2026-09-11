"""资金流本地逐日累积缓存（2026-09-11）。

背景：20 日历史资金流原走 push2his，本网络下 push2/push2his 被**主机级拦截**
（改 UA/Referer 无效），TA 的镜像降级 push2delay 对历史接口无能力（daykline 仅当日
1 行、kline 0 行），datacenter 亦无对应报表 → 只能本地逐日累积（"自给"）。
原实现还会把「降级成 1 行」静默输出为 `last 1 trading days`，读起来像"本就只有 1 天"。
"""
import csv
import datetime as _dt


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.reader(f))


def _patch_cache(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = tmp_path / "fund_flow_daily.csv"
    monkeypatch.setattr(a_stock, "_fund_flow_cache_path", lambda: str(path))
    return path


def test_save_flow_snapshot_sorted_dedup_and_nan(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = _patch_cache(monkeypatch, tmp_path)
    a_stock._save_fund_flow_snapshot(
        "2026-09-02", "600938",
        {"main": 100, "large": 50, "mid": -1, "small": -2, "super": -3})
    a_stock._save_fund_flow_snapshot(
        "2026-09-01", "600938",
        {"main": 10, "large": None, "mid": 1, "small": 2, "super": 3})
    a_stock._save_fund_flow_snapshot(
        "2026-09-02", "600938",
        {"main": 999, "large": 50, "mid": -1, "small": -2, "super": -3})

    rows = _read(path)
    assert rows[0] == ["date", "code", "main", "large", "mid", "small", "super"]
    assert rows[1] == ["2026-09-01", "600938", "10", "nan", "1", "2", "3"]
    assert rows[2][2] == "999"  # 同日重写覆盖（去重），不新增行
    assert len(rows) == 3
    # nan 占位（缺字段）而不是 0，避免把「没取到」伪装成「净流入 0」
    assert rows[1][3] == "nan"


def test_save_flow_snapshot_atomic_no_tmp_leftover(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = _patch_cache(monkeypatch, tmp_path)
    a_stock._save_fund_flow_snapshot("2026-09-01", "600938", {"main": 1})
    assert path.is_file()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".fundflow_")]


def test_load_flow_history_filters_by_code_cutoff_and_n(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    for day, value in (("2026-09-01", 1), ("2026-09-02", 2), ("2026-09-03", 3)):
        a_stock._save_fund_flow_snapshot(day, "600938", {"main": value})
    a_stock._save_fund_flow_snapshot("2026-09-03", "000001", {"main": 42})

    rows = a_stock._load_fund_flow_history("600938")
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert rows[2]["main"] == 3
    assert [r["date"] for r in a_stock._load_fund_flow_history("600938", cutoff="2026-09-02")] \
        == ["2026-09-01", "2026-09-02"]
    assert len(a_stock._load_fund_flow_history("600938", n=2)) == 2
    assert a_stock._load_fund_flow_history("999999") == []


def test_load_flow_history_missing_file(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    assert a_stock._load_fund_flow_history("600938") == []


def _stub_market(monkeypatch, klines):
    from tradingagents.dataflows import a_stock

    class _Resp:
        def json(self):
            return {"data": {"klines": klines}}

    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _Resp())
    monkeypatch.setattr(a_stock, "_is_historical", lambda d: False)
    monkeypatch.setattr(a_stock, "_market_today", lambda: _dt.date(2026, 9, 11))


def test_get_fund_flow_merges_local_cache_and_warns_on_short_window(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    # 缓存存原始「元」，渲染时转「万元」
    a_stock._save_fund_flow_snapshot(
        "2026-09-10", "600938",
        {"main": 1110000, "large": 10000, "mid": 20000, "small": 30000, "super": 40000})
    # 外部历史接口只给当日 1 行（镜像 push2delay 的真实行为）
    _stub_market(monkeypatch, ["2026-09-11 14:51,138270000,1,2,3,4,5"])

    text = a_stock.get_fund_flow("600938", "2026-09-11")
    assert "last 2 trading days" in text          # 本地 1 天 + 外部 1 天
    assert "| main=111" in text                   # 本地行
    assert "| main=13827" in text                 # 外部行
    assert "注意：外部历史接口不可用或未覆盖" in text   # 降级显式化，不再静默
    assert "本地累积缓存 2 天" in text


def test_get_fund_flow_writes_today_snapshot(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    _stub_market(monkeypatch, ["2026-09-11 14:51,138270000,11,12,13,14"])

    a_stock.get_fund_flow("600938", "2026-09-11")
    rows = a_stock._load_fund_flow_history("600938")
    assert [r["date"] for r in rows] == ["2026-09-11"]
    assert rows[0]["main"] == 138270000 and rows[0]["super"] == 14


def test_get_fund_flow_history_failure_does_not_break_tool(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    a_stock._save_fund_flow_snapshot("2026-09-10", "600938", {"main": 1110000})

    class _Resp:
        def json(self):
            return {"data": {"klines": ["2026-09-11 14:51,138270000,1,2,3,4,5"]}}

    def _em(url, *args, **kwargs):
        # 只让历史接口失败（主站被拦的真实形态）；实时接口仍正常
        if "daykline" in str(url):
            raise RuntimeError("push2his unreachable")
        return _Resp()

    monkeypatch.setattr(a_stock, "_em_get", _em)
    monkeypatch.setattr(a_stock, "_is_historical", lambda d: False)
    monkeypatch.setattr(a_stock, "_market_today", lambda: _dt.date(2026, 9, 11))

    text = a_stock.get_fund_flow("600938", "2026-09-11")
    assert "Error fetching fund flow" not in text
    assert "Historical Daily Fund Flow" in text
    assert "| main=111" in text                   # 外部历史挂了，本地缓存仍给出历史


def test_get_fund_flow_historical_cutoff_excludes_future_rows(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _patch_cache(monkeypatch, tmp_path)
    for day, value in (("2026-09-08", 800000), ("2026-09-10", 1000000),
                       ("2026-09-11", 1100000)):
        a_stock._save_fund_flow_snapshot(day, "600938", {"main": value})

    class _Resp:
        def json(self):
            return {"data": {"klines": []}}

    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _Resp())
    monkeypatch.setattr(a_stock, "_is_historical", lambda d: True)
    monkeypatch.setattr(a_stock, "_market_today", lambda: _dt.date(2026, 9, 11))

    text = a_stock.get_fund_flow("600938", "2026-09-09")
    assert "截至 2026-09-09" in text
    assert "| main=80" in text
    assert "| main=100" not in text and "| main=110" not in text  # 分析日之后的不得出现
