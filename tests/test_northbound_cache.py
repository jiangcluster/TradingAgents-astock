"""北向资金缓存写入：多票并行深析时的并发安全（原子替换）。"""

import csv


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_save_northbound_snapshot_appends_sorted_and_dedups(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    # 乱序写入 → 文件按日期升序；同日重写覆盖（去重）
    a_stock._save_northbound_snapshot("2026-09-02", 1.0, 2.0)
    a_stock._save_northbound_snapshot("2026-09-01", 12.34, -5.6)
    a_stock._save_northbound_snapshot("2026-09-02", 9.9, 9.9)
    rows = _read(path)
    assert rows[0] == ["date", "hgt", "sgt"]
    assert rows[1:] == [
        ["2026-09-01", "12.34", "-5.60"],
        ["2026-09-02", "9.90", "9.90"],
    ]


def test_save_northbound_snapshot_atomic_no_tmp_leftover(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    a_stock._save_northbound_snapshot("2026-09-01", 1.0, 2.0)
    assert path.is_file()
    # 原子替换后不残留临时文件
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".northbound_")]


def test_save_northbound_snapshot_existing_history_preserved(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    path.write_text("date,hgt,sgt\n2026-08-28,100.00,-50.00\n", encoding="utf-8")
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    a_stock._save_northbound_snapshot("2026-09-01", 1.5, 2.5)
    rows = _read(path)
    assert rows[1:] == [
        ["2026-08-28", "100.00", "-50.00"],
        ["2026-09-01", "1.50", "2.50"],
    ]


def test_save_northbound_snapshot_sgt_none_writes_nan(monkeypatch, tmp_path):
    """A5：上游停止披露 sgt 时写 nan 占位，不得伪装成「净流入 0 亿」。"""
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    a_stock._save_northbound_snapshot("2026-09-01", -9.28, None)
    rows = _read(path)
    assert rows[1:] == [["2026-09-01", "-9.28", "nan"]]


def test_save_northbound_snapshot_sgt_none_keeps_other_days(monkeypatch, tmp_path):
    """nan 占位只影响当日行，既有含真实 sgt 的历史行不受污染。"""
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    path.write_text("date,hgt,sgt\n2026-08-28,100.00,-50.00\n", encoding="utf-8")
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    a_stock._save_northbound_snapshot("2026-09-01", 1.5, None)
    a_stock._save_northbound_snapshot("2026-09-02", 2.5, 3.5)
    rows = _read(path)
    assert rows[1:] == [
        ["2026-08-28", "100.00", "-50.00"],
        ["2026-09-01", "1.50", "nan"],
        ["2026-09-02", "2.50", "3.50"],
    ]


# ---------------------------------------------------------------------------
# 0.5.39（批4）：上游停更识别（值连日恒定）+ 周末不写快照 + 交易日跳档说明
#   2026-09-28 实测：同花顺 hsgtApi 的 HGT 收盘值连续 12 个交易日恒为 −9.28
#   （当场重请求终值仍是 −9.28）→ 该源**整体**停更；此前报告把它当当日真实净流出
#   并给出 "Net northbound OUTFLOW (bearish)" 方向信号，属把坏值当证据。
# ---------------------------------------------------------------------------
def _write_history(monkeypatch, tmp_path, rows):
    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    body = "date,hgt,sgt\n" + "".join(f"{d},{h},{s}\n" for d, h, s in rows)
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    return path


def test_stale_note_flags_frozen_value(monkeypatch, tmp_path):
    from datetime import date

    from tradingagents.dataflows import a_stock

    _write_history(monkeypatch, tmp_path, [
        ("2026-09-24", -9.28, "nan"), ("2026-09-23", -9.28, "nan"),
        ("2026-09-22", -9.28, "nan"), ("2026-09-21", -25.0, "nan")])
    monkeypatch.setattr(a_stock, "_market_today", lambda: date(2026, 9, 28))
    note = a_stock._northbound_stale_note(-9.28)
    assert a_stock._NB_STALE_MARKER in note
    assert "完全相同" in note and "不得" in note


def test_stale_note_absent_below_threshold(monkeypatch, tmp_path):
    from datetime import date

    from tradingagents.dataflows import a_stock

    _write_history(monkeypatch, tmp_path, [
        ("2026-09-24", -9.28, "nan"), ("2026-09-23", -25.0, "nan")])
    monkeypatch.setattr(a_stock, "_market_today", lambda: date(2026, 9, 28))
    assert a_stock._northbound_stale_note(-9.28) == ""


def test_stale_note_absent_when_values_vary(monkeypatch, tmp_path):
    from datetime import date

    from tradingagents.dataflows import a_stock

    _write_history(monkeypatch, tmp_path, [
        ("2026-09-24", -9.28, "nan"), ("2026-09-23", -9.28, "nan"),
        ("2026-09-22", -9.28, "nan")])
    monkeypatch.setattr(a_stock, "_market_today", lambda: date(2026, 9, 28))
    # 今日值不同 → 说明源仍在更新，不得误报停更
    assert a_stock._northbound_stale_note(-3.5) == ""


def test_stale_note_ignores_today_row_in_history(monkeypatch, tmp_path):
    """今日行（同值）不得被算进"连日同值"——否则每天都自证停更。"""
    from datetime import date

    from tradingagents.dataflows import a_stock

    _write_history(monkeypatch, tmp_path, [
        ("2026-09-28", -9.28, "nan"), ("2026-09-25", -9.28, "nan")])
    monkeypatch.setattr(a_stock, "_market_today", lambda: date(2026, 9, 28))
    assert a_stock._northbound_stale_note(-9.28) == ""


def test_stale_note_survives_broken_history(monkeypatch, tmp_path):
    from datetime import date

    from tradingagents.dataflows import a_stock

    path = tmp_path / "northbound_daily.csv"
    path.write_text("date,hgt,sgt\n2026-09-24,garbage,nan\n", encoding="utf-8")
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    monkeypatch.setattr(a_stock, "_market_today", lambda: date(2026, 9, 28))
    assert a_stock._northbound_stale_note(-9.28) == ""      # 不抛异常


def test_weekend_snapshot_not_written(monkeypatch, tmp_path):
    """周末不写快照（2026-09-19 周六实测被写入一行 → 伪造样本 + 污染停更基线）。"""
    import requests

    from tradingagents.dataflows import a_stock

    path = _write_history(monkeypatch, tmp_path, [("2026-09-18", -7.0, "nan")])
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(a_stock, "_market_today", lambda: __import__("datetime").date(2026, 9, 19))

    class _Resp:
        @staticmethod
        def json():
            return {"time": ["09:30", "15:00"], "hgt": [0.0, -9.28], "sgt": [1.0, 2.0]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    out = a_stock.get_northbound_flow("2026-09-19")
    assert path.read_text(encoding="utf-8") == before        # 未写入
    assert "未写入本地快照" in out and "周末" in out


# ---------------------------------------------------------------------------
# 交易日跳档说明（K 线数据块）——防止把休市日误报成"数据源缺失"
# ---------------------------------------------------------------------------
def test_gap_note_silent_for_normal_weekend():
    from tradingagents.dataflows import a_stock

    # 周五 → 周一：纯周末休市，不该每天刷噪声
    assert a_stock._trading_day_gap_note(
        ["2026-09-18", "2026-09-21"]) == ""


def test_gap_note_flags_holiday_style_gap():
    from tradingagents.dataflows import a_stock

    note = a_stock._trading_day_gap_note(["2026-09-24", "2026-09-28"])
    assert "日期跳档说明" in note
    assert "2026-09-24→2026-09-28" in note
    assert "无法仅凭序列区分" in note
    assert "请勿据此断言" in note


def test_gap_note_handles_bad_and_short_input():
    from tradingagents.dataflows import a_stock

    assert a_stock._trading_day_gap_note([]) == ""
    assert a_stock._trading_day_gap_note(["2026-09-28"]) == ""
    assert a_stock._trading_day_gap_note(["junk", "2026-09-28"]) == ""


def test_gap_note_limits_list_length():
    from tradingagents.dataflows import a_stock

    dates = ["2026-01-02", "2026-01-07", "2026-01-12", "2026-01-16", "2026-01-21"]
    note = a_stock._trading_day_gap_note(dates)
    assert note.count("→") <= 3


# ---------------------------------------------------------------------------
# 读侧跳过周末行（0.5.39 批5）：脏样本不进停更判定基线/历史表
# ---------------------------------------------------------------------------
def test_history_read_skips_weekend_rows(monkeypatch, tmp_path):
    from tradingagents.dataflows import a_stock

    _write_history(monkeypatch, tmp_path, [
        ("2026-09-18", -7.0, "nan"),
        ("2026-09-19", -9.28, "nan"),      # 周六（脏样本）
        ("2026-09-22", -3.0, "nan"),
    ])
    rows = a_stock._load_northbound_history(20)
    assert [r[0] for r in rows] == ["2026-09-18", "2026-09-22"]


def test_is_weekend_date_safe():
    from tradingagents.dataflows import a_stock

    assert a_stock._is_weekend_date("2026-09-19") is True     # 周六
    assert a_stock._is_weekend_date("2026-09-20") is True     # 周日
    assert a_stock._is_weekend_date("2026-09-18") is False    # 周五
    assert a_stock._is_weekend_date("junk") is False          # 不误伤
    assert a_stock._is_weekend_date(None) is False
