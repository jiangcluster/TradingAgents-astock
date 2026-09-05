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
