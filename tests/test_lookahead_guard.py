"""未来函数防护（point-in-time）。

在历史日期上跑分析时，数据层不能把"今天"的数据当成"分析日当天"的事实交给模型
——报告里完全看不出来，但结论已经被污染了。上游 TradingAgents 把这类问题统称为
backtesting date fidelity（#475）。

本仓库审出三个函数收了日期参数却完全没用：`get_fund_flow`（今天的分钟资金流 +
从今天回溯 20 日）、`get_fundamentals`（腾讯实时估值）、`get_profit_forecast`
（当前一致预期）。前者能真正做时点截断；后两者的数据源根本不提供历史时点值，
补不上就必须**说出来**，而不是静默把今天的数字当历史事实。
"""

from datetime import datetime, timedelta
import os
import time

import pytest

from tradingagents.dataflows import a_stock


# 用市场时区定"今天"：主机时区不同会把当天的分析判成复盘，测试结论随之翻转。
TODAY = a_stock._market_today().isoformat()
PAST = (a_stock._market_today() - timedelta(days=90)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 判定本身
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (PAST, True),
        (TODAY, False),
        ((datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"), False),
        ("", False),
        (None, False),
        ("not-a-date", False),          # 解析不了不能当成历史，否则误伤实时分析
        (f"{PAST} 09:30:00", True),     # 带时分秒也要认得
    ],
)
def test_is_historical(value, expected):
    assert a_stock._is_historical(value) is expected


def test_snapshot_notice_names_the_date_and_says_do_not_use():
    notice = a_stock._snapshot_notice(PAST, "估值")

    assert PAST in notice
    assert "实时快照" in notice
    assert "不得" in notice   # 必须给模型明确指令，光提示"这是实时的"不够


# ---------------------------------------------------------------------------
# get_fund_flow：真正的时点截断
# ---------------------------------------------------------------------------


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def fake_em(monkeypatch):
    """假的东财返回：历史段跨越分析日前后，用来验证截断。"""
    calls = []

    def fake_get(url, params=None, timeout=10):
        calls.append(url)
        if "push2his" in url:
            return FakeResp({"data": {"klines": [
                "2026-05-01,1000,0,0,0,0,0",
                f"{PAST},2000,0,0,0,0,0",
                "2099-01-01,999999,0,0,0,0,0",   # 分析日之后 → 必须被剔除
            ]}})
        return FakeResp({"data": {"klines": [
            "2099-01-01 09:31,111,0,0,0,0,0",     # 实时段 → 复盘时整段不该取
        ]}})

    monkeypatch.setattr(a_stock, "_em_get", fake_get)
    return calls


def test_fund_flow_drops_rows_after_analysis_date(fake_em):
    out = a_stock.get_fund_flow("600519", PAST)

    assert "2099-01-01" not in out, "分析日之后的资金流泄漏了（未来函数）"
    assert PAST in out


def test_fund_flow_skips_realtime_when_historical(fake_em):
    a_stock.get_fund_flow("600519", PAST)

    assert not any("push2.eastmoney.com/api/qt/stock/fflow/kline" in u for u in fake_em), (
        "复盘历史日期时不该再去取今天的分钟资金流"
    )


def test_fund_flow_says_why_realtime_is_missing(fake_em):
    """略去实时段要说明原因，否则用户以为接口坏了。"""
    out = a_stock.get_fund_flow("600519", PAST)

    assert "略去实时分钟资金流" in out


def test_fund_flow_keeps_realtime_for_today(fake_em):
    """当天分析仍然要有实时资金流——防护不能误伤正常用法。"""
    a_stock.get_fund_flow("600519", TODAY)

    assert any("fflow/kline" in u for u in fake_em)


# ---------------------------------------------------------------------------
# 只有实时快照的两个：补不上就必须明说
# ---------------------------------------------------------------------------


def test_fundamentals_warns_on_historical_date(monkeypatch):
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_mootdx_call", lambda *a, **k: None)
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_fundamentals("600519", PAST)

    assert "未来函数警告" in out
    assert PAST in out


def test_fundamentals_silent_for_today(monkeypatch):
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_mootdx_call", lambda *a, **k: None)
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_fundamentals("600519", TODAY)

    assert "未来函数警告" not in out


def test_profit_forecast_warns_on_historical_date(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(
        a_stock, "_ths_eps_forecast",
        lambda code: pd.DataFrame({"年度": ["2026"], "预测每股收益": [1.23]}),
    )

    out = a_stock.get_profit_forecast("600519", PAST)

    assert "未来函数警告" in out


# ---------------------------------------------------------------------------
# codex 复审补：防护要在**生产调用路径**上真的生效
# ---------------------------------------------------------------------------


def test_profit_forecast_tool_exposes_curr_date():
    """工具不把 curr_date 传下去，数据层的未来函数告警就是死代码。

    v0.5.1 给 get_profit_forecast 加了告警，但 @tool 只暴露 ticker，
    curr_date 恒为 None → 告警永远不触发，模型照样把今天的一致预期当历史事实。
    """
    from tradingagents.agents.utils.agent_utils import get_profit_forecast

    assert "curr_date" in get_profit_forecast.args


def test_every_date_aware_tool_forwards_its_date():
    """凡是数据层按 curr_date 做时点处理的工具，@tool 都必须暴露并转发它。"""
    import inspect

    from tradingagents.agents.utils import signal_data_tools

    src = inspect.getsource(signal_data_tools)
    for name in ("get_profit_forecast", "get_fund_flow", "get_concept_blocks"):
        call = f'route_to_vendor("{name}", '
        idx = src.find(call)
        assert idx != -1, f"找不到 {name} 的路由调用"
        line = src[idx:src.find(")", idx)]
        assert "curr_date" in line, f"{name} 没有把 curr_date 转发给数据层"


def test_fund_flow_widens_window_for_older_dates(fake_em, monkeypatch):
    """分析日早于最近 20 个交易日时，必须放大回溯窗口。

    接口只提供"从今天回溯 lmt 天"，仍只要 20 天的话过滤后一行不剩——
    把"数据不对"变成"没有数据"，比不过滤更糟（codex P2）。
    """
    captured = {}
    orig = a_stock._em_get

    def spy(url, params=None, timeout=10):
        if "push2his" in url:
            captured["lmt"] = params.get("lmt")
        return orig(url, params=params, timeout=timeout)

    monkeypatch.setattr(a_stock, "_em_get", spy)
    a_stock.get_fund_flow("600519", PAST)      # PAST = 90 天前

    assert captured["lmt"] > 20, f"回溯窗口没有放大，仍是 {captured.get('lmt')}"


def test_fund_flow_says_when_history_is_unavailable(monkeypatch):
    """过滤后为空要说明原因，不能让正文凭空少一段。"""
    def empty_hist(url, params=None, timeout=10):
        return FakeResp({"data": {"klines": []}})

    monkeypatch.setattr(a_stock, "_em_get", empty_hist)
    out = a_stock.get_fund_flow("600519", PAST)

    assert "未能取到" in out


def test_profit_forecast_curr_date_is_required():
    """给默认值等于没设防：模型按 {"ticker": "600519"} 调用时 curr_date 为空串，
    判定为"非历史"，告警永远不触发（codex 终轮指出）。"""
    from tradingagents.agents.utils.agent_utils import get_profit_forecast

    required = get_profit_forecast.args_schema.model_json_schema().get("required", [])
    assert "curr_date" in required


def test_fundamentals_prompt_tells_the_model_to_pass_the_date():
    """工具签名要求了还不够——提示词不提，模型也不会主动传。"""
    import inspect

    from tradingagents.agents.analysts import fundamentals_analyst

    src = inspect.getsource(fundamentals_analyst.create_fundamentals_analyst)
    assert "get_profit_forecast(ticker, curr_date)" in src
    assert "curr_date 必须传" in src


def test_fund_flow_history_trimmed_to_twenty_rows(monkeypatch):
    """窗口为够回溯才放大，过滤后要裁回承诺的 20 个交易日（codex 终轮指出）。

    不裁的话复盘 90 天前会返回约 40 行，既改变了请求的趋势窗口，又把返回体撑大一倍。
    """
    rows = [f"2026-{m:02d}-{d:02d},1000,0,0,0,0,0"
            for m in (3, 4, 5) for d in range(1, 16)]   # 45 行，全部早于 PAST

    def fake_get(url, params=None, timeout=10):
        if "push2his" in url:
            return FakeResp({"data": {"klines": rows}})
        return FakeResp({"data": {"klines": []}})

    monkeypatch.setattr(a_stock, "_em_get", fake_get)
    out = a_stock.get_fund_flow("600519", PAST)

    kept = [ln for ln in out.splitlines() if ln.strip().startswith("2026-")]
    assert len(kept) == 20, f"应裁到 20 行，实际 {len(kept)} 行"


def test_is_historical_uses_market_timezone_not_host(monkeypatch):
    """"今天"必须按 A 股市场时区算，不能用主机本地时区（codex 第四轮）。

    主机在 UTC+9 以东（如新西兰 UTC+13）时，当地已过零点而上海还在前一天——
    当天的分析会被判成"复盘历史"，实时资金流被略去、快照工具打出莫须有的
    未来函数警告。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    class FakeDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            # 奥克兰已是 8-10 凌晨，上海仍是 8-09 晚间
            aware = _dt(2026, 8, 9, 23, 30, tzinfo=_tz(_td(hours=8)))
            return aware.astimezone(tz) if tz else aware.astimezone(_tz(_td(hours=13))).replace(tzinfo=None)

    monkeypatch.setattr(a_stock, "datetime", FakeDatetime)

    assert a_stock._market_today().isoformat() == "2026-08-09"
    # 市场当天不该被判成历史，哪怕主机日历已经翻页
    assert a_stock._is_historical("2026-08-09") is False
    assert a_stock._is_historical("2026-08-08") is True


def test_get_hot_stocks_empty_date_uses_market_today(monkeypatch):
    """第 5 类命中（0.5.35）：`get_hot_stocks("")` 的"今天"必须按**市场时区**算。

    此前回落 `datetime.now()`（主机本地日期）→ 主机在 UTC+8 以东时请求的是**第二天**，
    同花顺返回空，正文写成"当日无涨停/无题材"——与"该日确实没有"完全同形
    （同 `_is_historical` 那一条，判据统一走 `_market_today()`）。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    import requests

    class FakeDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            # 奥克兰已是 8-10 凌晨，上海仍是 8-09 晚间
            aware = _dt(2026, 8, 9, 23, 30, tzinfo=_tz(_td(hours=8)))
            return (aware.astimezone(tz) if tz
                    else aware.astimezone(_tz(_td(hours=13))).replace(tzinfo=None))

    monkeypatch.setattr(a_stock, "datetime", FakeDatetime)
    seen = {}

    class FakeResp:
        def json(self):
            return {"errocode": 0, "data": []}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return FakeResp()

    monkeypatch.setattr(requests, "get", fake_get)

    out = a_stock.get_hot_stocks("")

    assert "date/2026-08-09/" in seen["url"], f"用了主机日期而非市场日期：{seen['url']}"
    assert "2026-08-09" in out


# ---------------------------------------------------------------------------
# 补防护：北向 / 全球资讯 / 行业对比 / 概念板块 / 解禁（此前都收了日期却不用）
# ---------------------------------------------------------------------------


def test_date_is_after_compares_date_part_only():
    assert a_stock._date_is_after("2099-01-01 09:31", PAST) is True
    assert a_stock._date_is_after(PAST, PAST) is False
    # 解析不了不能删——格式意外不该把真实数据清空
    assert a_stock._date_is_after("", PAST) is False
    assert a_stock._date_is_after("unknown", PAST) is False
    assert a_stock._date_is_after(PAST, "") is False


def _patch_northbound_cache(monkeypatch, tmp_path, rows):
    cache = tmp_path / "northbound_daily.csv"
    body = "date,hgt,sgt\n" + "".join(f"{d},{h},{s}\n" for d, h, s in rows)
    cache.write_text(body, encoding="utf-8")
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(cache))
    return cache


def test_northbound_omits_realtime_on_historical_date(monkeypatch, tmp_path):
    """北向实时分钟段是"此刻"的数据，复盘历史时不能取、也不能写进当日快照。"""
    _patch_northbound_cache(monkeypatch, tmp_path, [])
    saved = []
    monkeypatch.setattr(
        a_stock, "_save_northbound_snapshot", lambda *a, **k: saved.append(a)
    )
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda *a, **k: pytest.fail("复盘历史日期时不该请求实时分钟接口"),
    )

    out = a_stock.get_northbound_flow(PAST)

    assert "未来函数警告" in out
    assert PAST in out
    assert "realtime minute series omitted" in out
    assert saved == []


def test_northbound_keeps_realtime_for_today(monkeypatch, tmp_path):
    """当天分析照常取实时数据——防护不能误伤正常用法。"""
    _patch_northbound_cache(monkeypatch, tmp_path, [])
    calls = []
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda url, **k: (
            calls.append(url)
            or FakeResp({"time": ["09:30"], "hgt": [1.5], "sgt": [2.5]})
        ),
    )
    monkeypatch.setattr(a_stock, "_save_northbound_snapshot", lambda *a, **k: None)

    out = a_stock.get_northbound_flow(a_stock._market_today().isoformat())

    assert calls, "当天分析必须继续取实时分钟数据"
    assert "未来函数警告" not in out
    assert "Realtime" in out


def test_northbound_history_excludes_rows_after_analysis_date(monkeypatch, tmp_path):
    """缓存里分析日之后才写入的收盘快照属于未来数据，不能进报告。"""
    _patch_northbound_cache(
        monkeypatch,
        tmp_path,
        [("2026-05-01", "1.00", "2.00"), ("2099-01-01", "9.00", "9.00")],
    )
    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: FakeResp({"time": []}))

    out = a_stock.get_northbound_flow(PAST, include_history=True)

    assert "2099-01-01" not in out, "历史段泄漏了分析日之后的北向收盘"
    assert "2026-05-01" in out


def test_northbound_snapshot_date_uses_market_timezone(monkeypatch, tmp_path):
    """快照写进哪天必须按市场时区算，否则键会错位到主机日历那一天。"""
    _patch_northbound_cache(monkeypatch, tmp_path, [])
    saved = {}
    monkeypatch.setattr(
        a_stock,
        "_save_northbound_snapshot",
        lambda d, h, s: saved.update(date=d),
    )
    monkeypatch.setattr(
        a_stock._requests, "get", lambda *a, **k: FakeResp({"time": ["09:30"], "hgt": [1.0], "sgt": []})
    )

    a_stock.get_northbound_flow(a_stock._market_today().isoformat())

    assert saved["date"] == a_stock._market_today().isoformat()


def _cls_news(title: str, iso_date: str) -> dict:
    ts = int(datetime.strptime(iso_date, "%Y-%m-%d").timestamp())
    return {"title": title, "content": "c", "ctime": ts}


def test_global_news_drops_items_published_after_analysis_date(monkeypatch):
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda *a, **k: FakeResp(
            {"data": {"roll_data": [_cls_news("当日旧闻", PAST), _cls_news("未来闻", "2099-01-01")]}}
        ),
    )
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_global_news(PAST)

    assert "当日旧闻" in out
    assert "未来闻" not in out, "分析日之后发布的资讯泄漏了"
    assert "已剔除" in out


def test_global_news_explains_when_rolling_window_has_nothing(monkeypatch):
    """实时源只滚动最新条目：全被剔除时要说明，不能拿今天的新闻冒充历史。"""
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda *a, **k: FakeResp({"data": {"roll_data": [_cls_news("未来闻", "2099-01-01")]}}),
    )
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_global_news(PAST)

    assert "No global news available" in out
    assert PAST in out


def test_global_news_keeps_everything_for_today(monkeypatch):
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda *a, **k: FakeResp({"data": {"roll_data": [_cls_news("今日新闻", TODAY)]}}),
    )
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_global_news(TODAY)

    assert "今日新闻" in out
    assert "已剔除" not in out


def test_industry_comparison_warns_on_historical_date(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_industry_comparison("600519", PAST)

    assert "未来函数警告" in out
    assert PAST in out


def test_industry_comparison_silent_for_today(monkeypatch):
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp({}))

    out = a_stock.get_industry_comparison("600519", TODAY)

    assert "未来函数警告" not in out


def _patch_concept_blocks(monkeypatch):
    monkeypatch.setattr(
        a_stock._requests,
        "get",
        lambda *a, **k: FakeResp(
            {
                "ResultCode": "0",
                "Result": {
                    "600519": [
                        {"name": "概念", "list": [{"name": "白酒", "ratio": "+1.2%"}]}
                    ]
                },
            }
        ),
    )


def test_concept_blocks_warns_on_historical_date(monkeypatch):
    """板块名单是事实、涨跌幅是快照；复盘历史时至少要标出后者不可当当日值。"""
    _patch_concept_blocks(monkeypatch)

    out = a_stock.get_concept_blocks("600519", PAST)

    assert "未来函数警告" in out
    assert "白酒" in out, "名单本身仍应保留（该拿的事实不能一起丢）"


def test_concept_blocks_silent_for_today_and_for_missing_date(monkeypatch):
    _patch_concept_blocks(monkeypatch)

    assert "未来函数警告" not in a_stock.get_concept_blocks("600519", TODAY)
    assert "未来函数警告" not in a_stock.get_concept_blocks("600519")


def test_lockup_history_excludes_unlocks_after_analysis_date(monkeypatch):
    """接口按解禁日倒序返回，不区分是否已过——未来解禁不能混进"历史解禁记录"。"""
    rows = [
        {"FREE_DATE": "2099-01-01", "LIMITED_STOCK_TYPE": "定增",
         "FREE_SHARES_NUM": 1, "FREE_RATIO": 1},
        {"FREE_DATE": "2026-05-01", "LIMITED_STOCK_TYPE": "首发",
         "FREE_SHARES_NUM": 2, "FREE_RATIO": 2},
    ]

    def fake_dc(report, filter_str="", **kw):
        if "FREE_DATE>=" in filter_str:
            return [r for r in rows if r["FREE_DATE"] >= PAST]
        return rows

    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", fake_dc)

    out = a_stock.get_lockup_expiry("600519", PAST)
    history_section = out.split("## 未来")[0]

    assert "共 1 批" in history_section, f"历史解禁未按分析日截断:\n{history_section}"
    assert "2099-01-01" not in history_section
    assert "2026-05-01" in history_section


def test_financial_statement_tools_require_curr_date():
    """给默认值等于没设防：模型不传 curr_date 时数据层不做任何时点截断。"""
    from tradingagents.agents.utils import fundamental_data_tools as tools

    for name in ("get_balance_sheet", "get_cashflow", "get_income_statement"):
        required = getattr(tools, name).args_schema.model_json_schema().get("required", [])
        assert "curr_date" in required, f"{name} 的 curr_date 仍是可选"


def test_financial_statement_tools_forward_vendor_order():
    """工具层把 curr_date 提到第二位只是为了必填，转发时必须还原数据层的位置顺序。"""
    import inspect

    from tradingagents.agents.utils import fundamental_data_tools as tools

    src = inspect.getsource(tools)
    for name in ("get_balance_sheet", "get_cashflow", "get_income_statement"):
        assert f'route_to_vendor("{name}", ticker, freq, curr_date)' in src


def test_statement_flags_missing_curr_date(monkeypatch):
    """缺 curr_date 时截断失效是静默的，必须在报告头里说出来。"""
    import pandas as pd

    monkeypatch.setattr(
        a_stock,
        "_get_financial_report_sina",
        lambda *a, **k: pd.DataFrame({"科目": ["货币资金"], "2026-06-30": [1.0]}),
    )

    assert "未提供分析日期" in a_stock.get_balance_sheet("600519")
    assert "未提供分析日期" not in a_stock.get_balance_sheet("600519", curr_date=PAST)


def test_atomic_write_replaces_and_cleans_up(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("old", encoding="utf-8")

    a_stock._atomic_write(str(path), lambda f: f.write("new"))

    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".x.csv.*")) == [], "临时文件没有清理干净"


def test_atomic_write_keeps_original_when_write_fails(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("old", encoding="utf-8")

    def boom(f):
        f.write("partial")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        a_stock._atomic_write(str(path), boom)

    assert path.read_text(encoding="utf-8") == "old", "写失败不该破坏已有缓存"
    assert list(tmp_path.glob(".x.csv.*")) == []


def test_cache_lock_acquires_and_releases(tmp_path):
    path = str(tmp_path / "c.csv")

    with a_stock._cache_lock(path) as acquired:
        assert acquired is True
        assert os.path.exists(path + ".lock")

    assert not os.path.exists(path + ".lock"), "锁文件没有释放"


def test_cache_lock_takes_over_stale_lock(tmp_path):
    """持锁进程被 kill 会留下锁文件，不能让后续所有取数都卡到超时。"""
    path = str(tmp_path / "c.csv")
    lock = path + ".lock"
    with open(lock, "w", encoding="utf-8"):
        pass
    stale = time.time() - 3600
    os.utime(lock, (stale, stale))

    with a_stock._cache_lock(path, timeout=1.0) as acquired:
        assert acquired is True


def test_cache_lock_degrades_instead_of_blocking(tmp_path):
    """抢不到锁要退化为无锁读写，不能因为缓存争用把取数流程打断。"""
    path = str(tmp_path / "c.csv")
    with open(path + ".lock", "w", encoding="utf-8"):
        pass

    started = time.monotonic()
    with a_stock._cache_lock(path, timeout=0.3) as acquired:
        assert acquired is False
    assert time.monotonic() - started < 3


def test_northbound_snapshot_merges_instead_of_overwriting(monkeypatch, tmp_path):
    """读-改-写：已有日期必须保留，否则并发深析会一天天丢缓存。"""
    cache = _patch_northbound_cache(monkeypatch, tmp_path, [("2026-05-01", "1.00", "2.00")])

    a_stock._save_northbound_snapshot("2026-05-02", 3.0, 4.0)

    body = cache.read_text(encoding="utf-8")
    assert "2026-05-01" in body and "2026-05-02" in body
    assert not os.path.exists(str(cache) + ".lock")
