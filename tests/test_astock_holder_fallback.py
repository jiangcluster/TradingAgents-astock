"""股东数据回退（2026-09-11）：mootdx F10 不可用时改用东财 datacenter。

背景：mootdx 走通达信 TCP 7709 协议，本网络下 14 台服务器「端口能连上但协议握手/
取数被拒」，原实现无回退 → 游资追踪师/解禁监控师分别报「内部人交易数据缺失（接口
不可用）」「前十大股东完整明细缺失」。
"""


def _fake_datacenter(rows_by_report):
    def _call(report, **kwargs):
        return rows_by_report.get(report, [])

    return _call


def test_datacenter_holder_sections_renders_latest_period_sorted(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(
        a_stock,
        "_eastmoney_datacenter",
        _fake_datacenter({
            "RPT_F10_EH_FREEHOLDERS": [
                {"HOLDER_RANK": 2, "HOLDER_NAME": "乙", "HOLD_NUM": 200,
                 "END_DATE": "2026-06-30 00:00:00", "FREE_HOLDNUM_RATIO": 1.5,
                 "HOLD_NUM_CHANGE": "增持"},
                {"HOLDER_RANK": 1, "HOLDER_NAME": "甲", "HOLD_NUM": 1000,
                 "END_DATE": "2026-06-30 00:00:00", "FREE_HOLDNUM_RATIO": 10.0,
                 "HOLD_NUM_CHANGE": "不变"},
                {"HOLDER_RANK": 1, "HOLDER_NAME": "旧期股东", "HOLD_NUM": 1,
                 "END_DATE": "2026-03-31 00:00:00", "FREE_HOLDNUM_RATIO": 0.1,
                 "HOLD_NUM_CHANGE": "不变"},
            ],
            "RPT_HOLDERNUMLATEST": [
                {"END_DATE": "2026-06-30 00:00:00", "HOLDER_NUM": 211367,
                 "PRE_HOLDER_NUM": 216267, "HOLDER_NUM_CHANGE": -4900,
                 "HOLDER_NUM_RATIO": -2.27, "AVG_HOLD_NUM": 14146.0},
            ],
            "RPT_SHARE_HOLDER_INCREASE": [
                {"NOTICE_DATE": "2026-03-05 00:00:00", "HOLDER_NAME": "集团",
                 "DIRECTION": "增持", "CHANGE_NUM": 70.55, "HOLD_RATIO": 1.6,
                 "TRADE_AVERAGE_PRICE": 42.532},
            ],
        }),
    )

    text = "\n".join(a_stock._datacenter_holder_sections("600938"))
    assert "十大流通股东（报告期 2026-06-30）" in text
    assert "1 | 甲 | 1,000 | 10.00 | 不变" in text   # 按排名排序 + 千分位
    assert "2026-03-31" not in text                   # 只保留最新报告期
    assert "股东户数" in text and "211,367" in text
    assert "大股东增减持" in text and "42.53" in text


def test_datacenter_holder_sections_empty_when_no_rows(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", _fake_datacenter({}))
    assert a_stock._datacenter_holder_sections("600938") == []


def test_get_insider_transactions_prefers_datacenter(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_datacenter_holder_sections",
                        lambda code: ["\n## 十大股东（报告期 2026-06-30）", "  1 | 甲"])

    def _unexpected(*args, **kwargs):
        raise AssertionError("datacenter 有数据时不应再调用 mootdx（协议层被拒会白等）")

    monkeypatch.setattr(a_stock, "_mootdx_call", _unexpected)
    out = a_stock.get_insider_transactions("600938")
    assert "东财 datacenter" in out
    assert "## 十大股东（报告期 2026-06-30）" in out


def test_get_insider_transactions_falls_back_to_mootdx(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_datacenter_holder_sections", lambda code: [])
    monkeypatch.setattr(a_stock, "_mootdx_call",
                        lambda *a, **k: "【4.股东变化】\n甲 100 股")
    out = a_stock.get_insider_transactions("600938")
    assert "mootdx F10" in out
    assert "【4.股东变化】" in out


def test_get_insider_transactions_datacenter_exception_falls_back(monkeypatch):
    from tradingagents.dataflows import a_stock

    def _boom(code):
        raise RuntimeError("datacenter down")

    monkeypatch.setattr(a_stock, "_datacenter_holder_sections", _boom)
    monkeypatch.setattr(a_stock, "_mootdx_call",
                        lambda *a, **k: "【4.股东变化】\n甲 100 股")
    out = a_stock.get_insider_transactions("600938")
    assert "【4.股东变化】" in out
