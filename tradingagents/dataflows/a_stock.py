"""A-stock (China mainland) data vendor for TradingAgents.

Zero third-party data dependency (no akshare). All sources are direct HTTP APIs
or mootdx TCP.

Data sources:
- mootdx (TCP 7709): OHLCV K-lines, financial snapshots, F10 text
- Tencent Finance (HTTP GBK): PE/PB/market cap/turnover
- 东方财富 push2 / datacenter-web (direct HTTP): stock info, dragon-tiger, lockup
- 新浪财经 (direct HTTP): K-line fallback, financial statements
- 同花顺 (direct HTTP): consensus EPS, hot stocks, northbound capital flow
- 财联社 (direct HTTP): global news wire
"""

from __future__ import annotations

from typing import Annotated
from datetime import date, datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
import contextlib
import json as _json
import os
import logging
from io import StringIO
import math
import random
import re as _re
import socket
import time
import uuid
import urllib.request
from urllib.parse import urlsplit

import pandas as pd
import requests as _requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: ticker format & market detection
# ---------------------------------------------------------------------------

def _get_prefix(code: str) -> str:
    """6-digit A-stock code -> market prefix for Tencent API.

    The 92 prefix must be checked before the leading-9 rule: the Beijing Stock
    Exchange started issuing 920xxx codes for new listings in October 2024, and
    a bare ``startswith("9")`` routes them to Shanghai, where the Tencent quote
    endpoint returns an empty payload (issue #85).  Only 900xxx (Shanghai B
    shares) legitimately belongs to ``sh``.
    """
    if code.startswith("92"):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


def _reject_non_a_share(original: str, code: str) -> None:
    """港股/美股代码走到 A 股数据层时当场报错，而不是拿去查 A 股（#43）。

    A 股代码恒为 6 位数字。港股是 4~5 位（`00700`）或带 `.HK` 后缀，美股是字母。
    这些代码此前会被**原样放行**，然后拿去问 mootdx / 腾讯 / 东财——而这些源对
    不存在的代码往往不报错，只返回空值或僵尸报价（北交所 920 号段就踩过，见
    `_normalize_ticker` 上游的 `_get_prefix`）。于是模型会拿着一份看起来正常、
    实际属于别的市场或根本不存在的数据写完整篇报告，报告里完全看不出来。
    """
    if code.isdigit() and len(code) == 6:
        return
    upper = original.strip().upper()
    if upper.endswith(".HK") or (code.isdigit() and len(code) in (4, 5)):
        raise ValueError(
            f"'{original}' 是港股代码。本数据层只支持 A 股（6 位数字代码，"
            f"如 600519 / 000001）。港股数据请用姊妹项目 global-stock-data，"
            f"多 Agent 港股分析仍在 roadmap（issue #43）。"
        )
    if code and not code.isdigit():
        raise ValueError(
            f"'{original}' 不是 A 股代码。本数据层只支持 A 股 6 位数字代码"
            f"（如 600519）；美股/港股请用姊妹项目 global-stock-data。"
        )
    raise ValueError(
        f"'{original}' 不是有效的 A 股代码：A 股代码恒为 6 位数字（如 600519），"
        f"这里解析出的是 '{code}'。"
    )


def _normalize_ticker(symbol: str) -> str:
    """Strip exchange prefix/suffix, return pure 6-digit code.

    Handles: '688017', 'SH688017', '688017.SH', 'sh688017'

    非 A 股代码（港股 `00700` / `0700.HK`、美股 `AAPL`）会直接报错，不再原样
    放行去查 A 股数据源（#43）。
    """
    s = symbol.strip().upper()
    # Remove .SH / .SZ / .BJ suffix
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Remove SH / SZ / BJ prefix
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    code = safe_ticker_component(s)
    _reject_non_a_share(symbol, code)
    return code


# ---------------------------------------------------------------------------
# Stock name <-> code mapping (cached)
# ---------------------------------------------------------------------------

_name_to_code: dict[str, str] | None = None
_code_to_name: dict[str, str] | None = None


def _build_name_code_map() -> tuple[dict[str, str], dict[str, str]]:
    """Build name→code and code→name maps via mootdx (both SH & SZ markets)."""
    global _name_to_code, _code_to_name
    if _name_to_code is not None:
        return _name_to_code, _code_to_name

    n2c: dict[str, str] = {}
    c2n: dict[str, str] = {}

    try:
        for market in (0, 1):  # 0=SZ, 1=SH
            stocks = _mootdx_call("stocks", market=market)
            if stocks is None or stocks.empty:
                continue
            for _, row in stocks.iterrows():
                code = str(row["code"]).strip()
                name = str(row["name"]).strip()
                if not _re.match(r"^[036]\d{5}$", code):
                    continue
                clean_name = name.replace(" ", "").replace("　", "")
                n2c[clean_name] = code
                c2n[code] = clean_name
    except Exception as e:
        # 网络抖动/通达信不可达时给出明确提示，而非冒泡成风马牛不相及的报错（#46/#66）
        raise ValueError(
            "无法通过 mootdx 解析股票名称（通达信服务暂时不可达）：%s。"
            "请稍后重试，或直接输入 6 位股票代码。" % e
        ) from e

    _name_to_code = n2c
    _code_to_name = c2n
    logger.info("Built stock name-code map: %d entries", len(n2c))
    return _name_to_code, _code_to_name


def resolve_ticker(user_input: str) -> str:
    """Resolve user input (code or Chinese name) to a 6-digit A-stock code.

    Accepts: '600379', 'SH600379', '600379.SH', '宝光股份'
    Returns: '600379'
    Raises: ValueError if not resolvable.
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    has_chinese = any("一" <= ch <= "鿿" for ch in s)

    if not has_chinese:
        return _normalize_ticker(s)

    clean = s.replace(" ", "").replace("　", "")
    n2c, _ = _build_name_code_map()

    if clean in n2c:
        return n2c[clean]

    matches = {name: code for name, code in n2c.items() if clean in name}
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        examples = ", ".join(f"{n}({c})" for n, c in list(matches.items())[:5])
        raise ValueError(f"'{s}' 匹配到多只股票: {examples}，请输入完整名称或代码")

    # LLM 有时会把行业/概念名（如 '游戏'、'白酒'）当 ticker 传进来（#76）。
    # 报错必须写明原因和正确用法，让模型能在下一次工具调用中自我纠正。
    raise ValueError(
        f"找不到股票 '{s}'。ticker 参数只接受 6 位股票代码（如 '600519'）"
        f"或完整股票名称（如 '贵州茅台'）；行业/概念/板块名（如 '游戏'）不是"
        f"有效的股票标识。请改用目标个股的 6 位股票代码重试。"
    )


# ---------------------------------------------------------------------------
# 未来函数防护（point-in-time）
# ---------------------------------------------------------------------------


# A 股市场时区。判"今天"必须按市场所在地算，不能用主机本地时区——
# 主机在 UTC+9 以东（如新西兰 UTC+13）时，当地已过零点而上海还在前一天，
# 当天的分析会被判成"复盘历史"：实时资金流被略去、快照工具打出莫须有的未来函数
# 警告。反过来主机在西半球也会把已经过去的交易日当成"今天"。
_MARKET_TZ = timezone(timedelta(hours=8))


def _market_today() -> "date":
    """A 股市场当前日期（Asia/Shanghai），与主机时区无关。"""
    return datetime.now(_MARKET_TZ).date()


def _is_historical(curr_date) -> bool:
    """分析日期是否早于市场当天。早于 = 这次是在复盘历史，不能拿实时数据当事实。"""
    if not curr_date:
        return False
    try:
        return (
            datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").date()
            < _market_today()
        )
    except ValueError:
        return False


def _snapshot_notice(curr_date: str, what: str) -> str:
    """实时快照被用在历史日期上时，在正文顶部明说。

    有些数据源只提供"此刻"的值（腾讯实时行情、同花顺当前一致预期），拿不到
    某个历史日的原值。既然补不上，就必须**说出来**——否则模型会把今天的估值
    当成分析日当天的事实写进报告，而这种污染在报告里完全看不出来。
    """
    return (
        f"⚠️ 未来函数警告：以下{what}是**此刻的实时快照**，不是 {curr_date} 当天的值。"
        f"本数据源不提供历史时点数据。在复盘历史日期时，**不得**把这些数字当作"
        f"{curr_date} 当天已知的事实，也不要据此推断当时的判断。\n"
    )


# ---------------------------------------------------------------------------
# mootdx client (singleton)
# ---------------------------------------------------------------------------

_mootdx_client = None

# 实测可用的通达信备选服务器（按延迟排序，2026-06 验证）。用于规避 mootdx
# 0.11.x 全新安装时 BESTIP.HQ 为空串导致的 `ValueError: not enough values to unpack`。
_TDX_SERVERS = [
    ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
    ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
    ("123.60.70.228", 7709), ("124.71.9.153", 7709), ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


# 探测用的探针股票：主板老票，任何通达信服务器都应能返回它的日线。
_TDX_CANARY_SYMBOL = "600519"

# 全部服务器都验不过之后，隔多久才允许再探一轮（秒）。没有这个负缓存，
# 每一次取数都会把整张服务器表重探一遍（10 台 × TCP 超时），把"取不到数"
# 放大成"每个请求卡几十秒"。
_MOOTDX_RETRY_AFTER_S = 300.0
_mootdx_unavailable_until = 0.0

# ⚠️ 曾经加过「连续 N 台协议失败就停手」的提前退出，已移除：三台远端拒绝**证明不了**
# 本地网络封了协议，而列表里靠后的服务器完全可能是好的。提前收手会让那台可用服务器
# 永远试不到，还顺手记下 5 分钟负缓存。省下的十几秒不值得换这个风险——真正的耗时
# 大头是 bestip 全表测速，那个已经单独规避了。


def _candidate_tdx_servers() -> list[tuple[str, int]]:
    """待试的通达信服务器：先用实测精选的 `_TDX_SERVERS`，再补 mootdx 自带的完整主机表。

    只试精选的那 10 台是不够的——它们要是恰好都不可用，而 mootdx 自带表里还有活着的
    主机，就会被判成"全网不可达"并记 5 分钟负缓存。这里把两张表合起来去重后逐台验证，
    覆盖面等同 `bestip`，但不做它那套要跑几分钟的全表测速。
    """
    servers = list(_TDX_SERVERS)
    seen = set(servers)
    try:
        from mootdx.consts import HQ_HOSTS
        for entry in HQ_HOSTS:
            # 形如 ("深圳双线主站1", "110.41.147.114", 7709)
            host = (entry[1], entry[2]) if len(entry) >= 3 else None
            if host and host not in seen:
                seen.add(host)
                servers.append(host)
    except Exception as e:  # mootdx 版本变动导致取不到就只用精选表，不影响主流程
        logger.debug("读取 mootdx HQ_HOSTS 失败，仅使用内置精选表：%s", e)
    return servers


def _reachable_tdx_servers(servers, timeout: float = 2.0):
    """并发做 TCP 预筛，返回可连的那些（保持原顺序）。

    只是把"等超时"这件事并行化，不改变优先级：返回顺序仍是候选表顺序，所以实测
    精选的服务器依旧排在前面、依旧第一个被真实验证。
    """
    from concurrent.futures import ThreadPoolExecutor

    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(servers))) as pool:
        flags = list(pool.map(lambda s: _probe_tdx(s[0], s[1], timeout), servers))
    return [srv for srv, ok in zip(servers, flags) if ok]


def _probe_tdx(ip: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 握手探测通达信服务器端口是否开着。

    ⚠️ 只是**廉价预筛**，通过不代表能取到数：实测存在大量"TCP 三次握手成功、
    通达信协议握手立刻被 RST"的服务器。选服务器必须再走 `_tdx_client_works()`
    做一次真实取数验证（#90）。
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tdx_client_works(client) -> bool:
    """真实拉一根 K 线来验证这个 client 确实能取数。"""
    try:
        df = client.bars(symbol=_TDX_CANARY_SYMBOL, category=4, offset=1)
        return df is not None and not df.empty
    except Exception:
        return False


def reset_mootdx_client() -> None:
    """丢弃缓存的 client，让下一次调用重新选服务器。

    单例一旦钉在一台"当时能用、后来挂了"的服务器上，之后每次取数都失败降级且
    永远不会重选。数据调用发现 mootdx 出错时调它，下一次就能换一台（#90）。
    """
    global _mootdx_client, _mootdx_unavailable_until
    _mootdx_client = None
    _mootdx_unavailable_until = 0.0


@contextlib.contextmanager
def _preserve_mootdx_bestip():
    """探测期间保护 mootdx 的持久化服务器配置，退出时按需还原。

    `StdQuotes.__init__` 里有 `config.set('BESTIP', {'HQ': self.server})`——**每建一次
    带 server 的 client 都会写进 mootdx 的配置文件**。逐台探测 38 个候选就等于把用户
    原本配好的服务器一路覆写，最后留下的是最后一台**失败的**服务器，还会连累同一台
    机器上其它用 mootdx 的程序。

    🔴 必须先 `setup()` 再快照：新进程里 `config.get("BESTIP")` 返回的是模块默认空值，
    用户持久化的值要等 `BaseQuotes.__init__` 调 `setup()` 才读进来。快照到空值的话，
    "还原"反而会把真实配置抹成空——比不还原更糟。
    实测（mootdx 0.11.7）：setup 前 `{'HQ': ''}`，setup 后 `{'HQ': ['218.6.x.x', 7709]}`。

    用法：`with _preserve_mootdx_bestip() as keep:` —— 选出可用服务器时调 `keep()`
    表示"这次的覆写是我们想要的，别还原"；不调就在退出时还原。

    ⚠️ **做成上下文管理器而不是手动调还原函数**：此前是在两处分别调 `_restore_bestip()`，
    再加一条提前返回就会漏掉一处，而漏掉的后果是静默留下一台死服务器。
    """
    saved = None
    try:
        from mootdx import config as _cfg
        _cfg.setup()
        saved = _cfg.get("BESTIP")
        if isinstance(saved, dict):
            saved = dict(saved)
    except Exception as e:  # 版本差异导致取不到就跳过保护，别影响主流程
        logger.debug("读取 mootdx BESTIP 失败，本次探测不做保护：%s", e)

    keep = {"flag": False}
    try:
        yield lambda: keep.__setitem__("flag", True)
    finally:
        if saved is not None and not keep["flag"]:
            try:
                from mootdx import config as _cfg2
                _cfg2.set("BESTIP", saved)
            except Exception as e:
                logger.debug("恢复 mootdx BESTIP 失败：%s", e)


def _get_mootdx_client():
    """Lazy-init 健壮版 mootdx Quotes client（TCP 连接，可复用）。

    选服务器的顺序：内置服务器表（TCP 预筛 + 真实取数验证）→ bestip 测速 →
    裸 factory（老用户 config 里已有 IP）。每一级都必须真正取到数据才会被采用，
    避免把 client 钉死在一台"端口开着但协议不通"的服务器上（#90）。
    全部失败时抛 RuntimeError，并在 `_MOOTDX_RETRY_AFTER_S` 内直接快速失败，
    不再逐台重探。
    """
    global _mootdx_client, _mootdx_unavailable_until
    if _mootdx_client is not None:
        return _mootdx_client

    now = time.time()
    if now < _mootdx_unavailable_until:
        raise RuntimeError(
            "mootdx 通达信服务器暂不可用（%.0f 秒内不再重试）。"
            "已尝试全部内置服务器：端口能连上的也没能完成通达信协议取数。"
            "请检查网络环境（代理/防火墙/公司网络常拦 TCP 7709），"
            "或改用 6 位股票代码直接查询。" % (_mootdx_unavailable_until - now)
        )

    from mootdx.quotes import Quotes

    tcp_ok_but_dead = 0
    # 探测会覆写 mootdx 的持久化配置——包在这里，只有真选出可用服务器时才 keep()，
    # 其余每条退出路径（含异常）都自动还原。
    with _preserve_mootdx_bestip() as keep_bestip:
        # TCP 预筛并发跑：38 台里多数是"连都连不上"，串行每台要等满超时（实测整轮
        # 73.7s，首次调用像卡死）。预筛纯粹是等 IO，并发不改变选取语义——下面仍按
        # 原顺序、逐台做真实取数验证，精选表依旧优先。
        reachable = _reachable_tdx_servers(_candidate_tdx_servers())

        for ip, port in reachable:
            # 「TCP 通但通达信协议不通」有两种表现：factory 建连时握手就被拒，
            # 或者建出来了但取不到数。**两种都要算**——只统计后者的话，计数永远是 0
            # （实测这批服务器全是在 factory 里抛 ConnectionReset），下面的快速失败
            # 判断就失效了。
            try:
                candidate = Quotes.factory(market="std", server=(ip, port))
            except Exception as e:
                tcp_ok_but_dead += 1
                logger.debug("mootdx %s:%s 握手失败（%s），换下一台", ip, port, type(e).__name__)
            else:
                if _tdx_client_works(candidate):
                    logger.info("mootdx server selected: %s:%s", ip, port)
                    keep_bestip()   # 这次的覆写正是我们想要的，别还原
                    _mootdx_client = candidate
                    return _mootdx_client
                tcp_ok_but_dead += 1
                logger.debug("mootdx %s:%s 建连成功但取不到数，换下一台", ip, port)

    # 走到这里说明逐台探测都没成——上面的 with 已经把 BESTIP 还原成用户原本的配置，
    # 下面的裸 factory 读的正是它，这个兜底才有意义。
    # ⚠️ 刻意**不用** `bestip=True`：它会把整张主机表做一遍测速，实测要几分钟。
    # `_candidate_tdx_servers()` 已经把 mootdx 自带的完整主机表逐台验证过了，
    # 覆盖面不比 bestip 差，而且每台都是"真取到数才算通过"。
    try:
        candidate = Quotes.factory(market="std")
    except Exception as e:
        logger.debug("mootdx 裸 factory 失败 — %s", e)
    else:
        if _tdx_client_works(candidate):
            logger.info("mootdx client from 裸 factory（用户已有配置）")
            _mootdx_client = candidate
            return _mootdx_client

    _mootdx_unavailable_until = time.time() + _MOOTDX_RETRY_AFTER_S
    if tcp_ok_but_dead:
        # 说清楚是"协议被拒"而不是"连不上"——这两者的排查方向完全不同。
        cause = (
            "%d 台服务器端口能连上，但通达信协议握手/取数被拒。"
            "这通常是协议层被拦（代理、防火墙、公司网络对 TCP 7709 的策略），"
            "换服务器解决不了。" % tcp_ok_but_dead
        )
    else:
        cause = "内置服务器表里没有一台的 TCP 7709 能连上，请检查网络连通性。"
    raise RuntimeError(
        "mootdx 通达信服务器不可用：%s"
        "可改用 6 位股票代码直接查询。%.0f 秒内将直接快速失败、不再逐台重探。"
        % (cause, _MOOTDX_RETRY_AFTER_S)
    )


def _mootdx_call(method: str, **kwargs):
    """调用 mootdx 的某个方法，失败就弃用当前服务器。

    选中的服务器随时可能挂掉；不弃用的话单例会一直指着它，之后每次取数都失败降级
    且永不重选（#90 的「反复降级」）。取 client 本身失败时不清缓存——那条路径已经
    在 `_get_mootdx_client` 里做了负缓存，清掉等于取消快速失败。
    """
    client = _get_mootdx_client()
    try:
        return getattr(client, method)(**kwargs)
    except Exception:
        reset_mootdx_client()
        raise


# ---------------------------------------------------------------------------
# Tencent Finance API
# ---------------------------------------------------------------------------

def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch real-time quotes from Tencent Finance (qt.gtimg.cn).

    Returns dict[code] -> {name, price, pe_ttm, pe_dynamic, pb,
    mcap_yi(总市值), float_mcap_yi(流通市值), ...}
    """
    prefixed = [f"{_get_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")

    result = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # strip sh/sz/bj prefix
        result[code] = {
            "name": vals[1],
            "price": float(vals[3]) if vals[3] else 0,
            "last_close": float(vals[4]) if vals[4] else 0,
            "open": float(vals[5]) if vals[5] else 0,
            "change_pct": float(vals[32]) if vals[32] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            # 实测（工商银行 601398）vals[44]=21919.47 < vals[45]=28975.83，
            # 与东财 f116(总市值)/f117(流通市值) 交叉核对：44=流通市值、45=总市值。
            "float_mcap_yi": float(vals[44]) if vals[44] else 0,
            "mcap_yi": float(vals[45]) if vals[45] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            # vals[52] 经东财 datacenter RPT_VALUEANALYSIS_DET 显式命名字段终验：
            # 对应网页「市盈率(动)」（当期年化），非静态 PE；静态 PE 需年报归母净利，
            # 腾讯/东财现有字段均无法精确还原，故如实命名为 pe_dynamic。
            "pe_dynamic": float(vals[52]) if vals[52] else 0,
        }
    return result


# ---------------------------------------------------------------------------
# Eastmoney Datacenter unified helper (龙虎榜/解禁 etc.)
# ---------------------------------------------------------------------------

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# 东财防封：全局节流 + 会话复用 (Eastmoney anti-ban: throttle + Keep-Alive)
# ---------------------------------------------------------------------------
# 东财系 HTTP 接口（push2 / push2his / datacenter-web / search-api / np-weblist）
# 有风控：每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 次 / 5 分钟 ≥300 次 → 临时封 IP。
# 多 Agent 投研跑批量分析时会高频请求东财，是被封的头号元凶。所有 eastmoney.com
# 请求一律走 _em_get()：串行限流（最小间隔 + 随机抖动）+ 复用 Keep-Alive 会话 + 默认 UA。
# 注意：仅东财接口走此入口；mootdx(TCP) / 腾讯 / 新浪 / 同花顺 / 财联社 / 百度 等
# 不限流（实测不封 IP 或风控极弱）。批量任务可调大 EM_MIN_INTERVAL 进一步降速。
_EM_SESSION = _requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
# 两次东财请求最小间隔(秒)；批量多 Agent 场景可设环境变量 EM_MIN_INTERVAL=1.5~2 降速。
_EM_MIN_INTERVAL = float(os.environ.get("EM_MIN_INTERVAL", "1.0"))
_em_last_call = [0.0]  # 模块级上次东财请求时间戳

# push2 / push2his 主站在部分机房会被间歇性断连（RemoteDisconnected /
# ConnectionReset），而同源镜像 push2delay.eastmoney.com 实测稳定（K 线
# _em_kline_fallback、clist、fflow/kline 均已验证）。仅对这两个高频断连的
# 主机做镜像降级；datacenter / search-api-web 等不在表内，行为不变。
_EM_MIRROR = {
    "push2.eastmoney.com": "push2delay.eastmoney.com",
    "push2his.eastmoney.com": "push2delay.eastmoney.com",
}


def _em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA + 主站断连镜像降级。

    所有 eastmoney.com 接口都应通过它请求，避免多 Agent 高频拉数据被封 IP。
    串行限流：与上次东财请求间隔 < EM_MIN_INTERVAL 时 sleep 补足 + 0.1~0.5s 随机抖动。
    传入的 headers 会覆盖 session 默认 UA（用于保留各端点自己的 Referer/Origin）。
    push2 / push2his 主站连接失败或返回 4xx/5xx 时，自动用 push2delay 镜像重试一次。
    """
    wait = _EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))

    host = (urlsplit(url).hostname or "").lower()
    mirror_host = _EM_MIRROR.get(host)
    candidates = [url]
    if mirror_host:
        candidates.append(url.replace(host, mirror_host, 1))

    try:
        last_resp = None
        for idx, cand in enumerate(candidates):
            has_next = idx + 1 < len(candidates)
            try:
                resp = _EM_SESSION.get(
                    cand, params=params, headers=headers, timeout=timeout, **kwargs
                )
            except (
                _requests.exceptions.ConnectionError,
                _requests.exceptions.Timeout,
            ) as e:
                if has_next:
                    logger.warning(
                        "eastmoney %s failed (%s), fallback to mirror %s",
                        host, type(e).__name__, mirror_host,
                    )
                    continue
                raise
            last_resp = resp
            if resp.status_code < 400:
                return resp
            if has_next:
                logger.warning(
                    "eastmoney %s HTTP %s, fallback to mirror %s",
                    host, resp.status_code, mirror_host,
                )
                continue
            return resp
        return last_resp  # pragma: no cover - 循环必返回或抛出
    finally:
        _em_last_call[0] = time.time()


def _eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁 共用."""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = _em_get(_DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


def _fmt_int(value) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_wan(value) -> str:
    """元 → 万元（资金流字段渲染）。"""
    try:
        return f"{float(value) / 1e4:.0f}"
    except (TypeError, ValueError):
        return "—"


# 股东数据回退所用的东财 datacenter 报表（列已按线上实测响应命名）。
# mootdx F10 走通达信 TCP 协议，本网络下协议握手被拒；datacenter 为 HTTPS，
# 与龙虎榜（RPT_DAILYBILLBOARD_DETAILSNEW）/解禁（RPT_LIFT_STAGE）同源可用。
_HOLDER_REPORTS = (
    ("十大流通股东", "RPT_F10_EH_FREEHOLDERS", "FREE_HOLDNUM_RATIO", "占流通%"),
    ("十大股东", "RPT_F10_EH_HOLDERS", "HOLD_NUM_RATIO", "占比%"),
)


def _datacenter_holder_sections(code: str) -> list:
    """东财 datacenter 的股东维度数据（十大股东/流通股东/增减持/户数）。空列表=无数据。"""
    lines: list = []

    for title, report, ratio_col, ratio_label in _HOLDER_REPORTS:
        rows = _eastmoney_datacenter(
            report,
            columns=("HOLDER_RANK,HOLDER_NAME,HOLD_NUM,HOLD_NUM_CHANGE,"
                     f"END_DATE,{ratio_col}"),
            filter_str=f'(SECURITY_CODE="{code}")',
            page_size=30,
            sort_columns="END_DATE",
            sort_types="-1",
        )
        if not rows:
            continue
        latest = max(str(r.get("END_DATE") or "") for r in rows)
        period_rows = [r for r in rows if str(r.get("END_DATE") or "") == latest]
        period_rows.sort(key=lambda r: r.get("HOLDER_RANK") or 99)
        lines.append(f"\n## {title}（报告期 {latest[:10]}）")
        lines.append(f"排名 | 股东 | 持股(股) | {ratio_label} | 较上期变动")
        for r in period_rows[:10]:
            lines.append(
                f"  {r.get('HOLDER_RANK')} | {r.get('HOLDER_NAME')} | "
                f"{_fmt_int(r.get('HOLD_NUM'))} | {_fmt_num(r.get(ratio_col))} | "
                f"{r.get('HOLD_NUM_CHANGE') or '—'}"
            )

    num_rows = _eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST", columns="ALL",
        filter_str=f'(SECURITY_CODE="{code}")', page_size=5,
    )
    if num_rows:
        r = num_rows[0]
        lines.append(f"\n## 股东户数（报告期 {str(r.get('END_DATE') or '')[:10]}）")
        lines.append(
            f"  户数 {_fmt_int(r.get('HOLDER_NUM'))}（上期 "
            f"{_fmt_int(r.get('PRE_HOLDER_NUM'))}，变动 "
            f"{_fmt_int(r.get('HOLDER_NUM_CHANGE'))} 户 / "
            f"{_fmt_num(r.get('HOLDER_NUM_RATIO'))}%）；户均持股 "
            f"{_fmt_num(r.get('AVG_HOLD_NUM'), 0)} 股"
        )

    trade_rows = _eastmoney_datacenter(
        "RPT_SHARE_HOLDER_INCREASE", columns="ALL",
        filter_str=f'(SECURITY_CODE="{code}")', page_size=10,
        sort_columns="NOTICE_DATE", sort_types="-1",
    )
    if trade_rows:
        lines.append("\n## 大股东增减持（最近公告）")
        lines.append("公告日 | 股东 | 方向 | 变动(万股) | 持股比例% | 交易均价")
        for r in trade_rows[:10]:
            lines.append(
                f"  {str(r.get('NOTICE_DATE') or '')[:10]} | {r.get('HOLDER_NAME')} | "
                f"{r.get('DIRECTION') or '—'} | {_fmt_num(r.get('CHANGE_NUM'))} | "
                f"{_fmt_num(r.get('HOLD_RATIO'))} | {_fmt_num(r.get('TRADE_AVERAGE_PRICE'))}"
            )

    return lines


# ---------------------------------------------------------------------------
# 同花顺 EPS forecast helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """Fetch consensus EPS forecast from 同花顺 (direct HTTP).

    Returns DataFrame with columns roughly: 年度, 预测每股收益.
    注意：同花顺页面未给出机构数/区间，只有年度×EPS 的时间序列（SJ=实际值）。

    2026-09-06 修正：同花顺 worth.html 的盈利预测数据在 `id="yjycData"` 的
    内嵌 JSON 里（`[年份, EPS, 净利润, "SJ"]`，SJ=实际值、null=暂无预测），
    页面里唯一的 HTML 表格是「研报评级」图例（评级/说明两列），旧代码回退取
    `dfs[0]` 会把图例表当 EPS 表，产出 FY公司评级/EPS=0.0 之类的假数据。
    现改为直接从 `yjycData` 提取。
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    r = _requests.get(url, headers=headers, timeout=15)
    r.encoding = "gbk"
    html = r.text
    m = _re.search(r'id="yjycData"\s+class="none">(.*?)</div>', html, _re.S)
    if not m:
        return pd.DataFrame()
    try:
        rows = _json.loads(m.group(1))
    except (ValueError, TypeError):
        return pd.DataFrame()
    data = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        year = str(row[0]) if row[0] is not None else ""
        eps = row[1]
        if not year.isdigit() or eps is None:
            continue  # 过滤"暂无预测"的年份行
        try:
            data.append({"年度": year, "预测每股收益": float(eps)})
        except (TypeError, ValueError):
            continue
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# 东财 K-line fallback helper (direct HTTP via _em_get, 前复权日 K)
# ---------------------------------------------------------------------------


def _em_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily K-line from 东方财富 push2his as mootdx fallback.

    2026-09-04 接入：mootdx（通达信 TCP 7709）/ 新浪在部分网络时段不可用，
    东财 push2his（与 astock-data 同源、经 _em_get 节流）实测稳定。
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": "800",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    # push2his 主站会被部分机房间歇性断连；push2delay 镜像实测稳定（astock-data 同源降级）
    klines = []
    for host in ("push2his.eastmoney.com", "push2delay.eastmoney.com"):
        url = f"https://{host}/api/qt/stock/kline/get"
        try:
            r = _em_get(url, params=params, timeout=15)
            d = r.json()
            klines = ((d.get("data") or {}).get("klines")) or []
            if klines:
                break
        except Exception as e:
            logger.warning("eastmoney kline %s failed for %s: %s", host, code, e)
            continue
    rows = []
    for k in klines:
        p = k.split(",")
        if len(p) < 6:
            continue
        try:
            # 东财 kline 行格式（fields2=f51..f56）：
            #   p[0]日期 p[1]开盘 p[2]收盘 p[3]最高 p[4]最低 p[5]成交量(手)
            # 注意收盘在 p[2]、最高/最低在 p[3]/p[4]（非顺序 OHLC）。
            # 成交量单位为「手」，×100 换算为「股」以对齐新浪降级源。
            rows.append({
                "Date": p[0],
                "Open": float(p[1]),
                "Close": float(p[2]),
                "High": float(p[3]),
                "Low": float(p[4]),
                "Volume": float(p[5]) * 100,
            })
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df


# ---------------------------------------------------------------------------
# Tencent K-line fallback helper (web.ifzq.gtimg.cn, 与 astock-data 同源已验证)
# ---------------------------------------------------------------------------


def _tencent_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily 前复权 K-line from 腾讯 web.ifzq.gtimg.cn as fallback.

    行格式：[date, open, close, high, low, volume(手)]（注意收盘在 idx2）。
    成交量单位「手」，×100 换算为「股」以对齐新浪/东财降级源。
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = _get_prefix(code)
    prefixed = f"{prefix}{code}"
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={prefixed},day,,,800,qfq"
    )
    r = _requests.get(url, timeout=15, headers={"User-Agent": _UA})
    r.raise_for_status()
    d = _json.loads(r.text)
    node = ((d.get("data") or {}).get(prefixed)) or {}
    klines = node.get("qfqday") or node.get("day") or []

    rows = []
    for k in klines:
        try:
            date_str, open_s, close_s, high_s, low_s, vol_s = k[:6]
            rows.append({
                "Date": date_str,
                "Open": float(open_s),
                "Close": float(close_s),
                "High": float(high_s),
                "Low": float(low_s),
                "Volume": float(vol_s) * 100,
            })
        except (TypeError, ValueError, IndexError):
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df


# ---------------------------------------------------------------------------
# Sina K-line fallback helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _sina_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily K-line from Sina HTTP API as mootdx fallback.

    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = "sh" if code.startswith("6") else "sz"
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{prefix}{code}",
        "scale": "240",  # daily
        "ma": "no",
        "datalen": "800",
    }
    r = _requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = _json.loads(r.text)

    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        rows.append({
            "Date": item["day"],
            "Open": float(item["open"]),
            "High": float(item["high"]),
            "Low": float(item["low"]),
            "Close": float(item["close"]),
            "Volume": int(item["volume"]),
        })

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]

    return df


def _last_ohlcv_date(df: pd.DataFrame) -> pd.Timestamp | None:
    """Return the latest OHLCV Date in a normalized dataframe."""
    if df is None or df.empty or "Date" not in df.columns:
        return None
    dates = pd.to_datetime(df["Date"], errors="coerce")
    if dates.dropna().empty:
        return None
    return dates.max().normalize()


def _normalize_ohlcv_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize OHLCV Date values to daily granularity."""
    if df is None or df.empty or "Date" not in df.columns:
        return df
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    return df.dropna(subset=["Date"])


def _needs_sina_supplement(df: pd.DataFrame, target_date: str | None) -> bool:
    """True when mootdx/cache data is older than the requested cutoff date."""
    if not target_date:
        return False
    last_date = _last_ohlcv_date(df)
    if last_date is None:
        return True
    target = pd.to_datetime(target_date).normalize()
    return last_date < target


def _merge_ohlcv(primary: pd.DataFrame, supplement: pd.DataFrame) -> pd.DataFrame:
    """Merge OHLCV frames, preferring supplement rows on duplicate dates."""
    frames = [frame for frame in (primary, supplement) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_ohlcv_dates(combined)
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("Date").reset_index(drop=True)
    return combined


def _supplement_stale_ohlcv_with_sina(
    code: str,
    df: pd.DataFrame,
    target_date: str | None,
    start_date: str | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Use Sina daily K-line to fill dates missing from mootdx/cache data."""
    if not _needs_sina_supplement(df, target_date):
        return df, False
    try:
        sina_df = _sina_kline_fallback(code, start_date, target_date)
    except Exception as e:
        logger.warning("sina K-line supplement failed for %s: %s", code, e)
        return df, False
    if sina_df.empty:
        return df, False
    merged = _merge_ohlcv(df, sina_df)
    return merged, _last_ohlcv_date(merged) != _last_ohlcv_date(df)


# ---------------------------------------------------------------------------
# OHLCV loading with cache (mootdx -> CSV)
# ---------------------------------------------------------------------------

def _load_ohlcv_astock(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV via mootdx, cache to CSV, filter by curr_date.

    Mirrors stockstats_utils.load_ohlcv but uses mootdx instead of yfinance.
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    from .config import get_config

    code = _normalize_ticker(symbol)
    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)

    cache_file = os.path.join(cache_dir, f"{code}-astock-daily.csv")

    if os.path.exists(cache_file):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if mtime.date() == datetime.now().date():
            data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
            data = _normalize_ohlcv_dates(data)
            data, supplemented = _supplement_stale_ohlcv_with_sina(
                code, data, curr_date, start_date=None
            )
            if supplemented:
                data.to_csv(cache_file, index=False, encoding="utf-8")
            cutoff = pd.to_datetime(curr_date)
            return data[data["Date"] <= cutoff]

    # Fetch from mootdx — 800 daily bars (~3 years of trading days)
    try:
        df = _mootdx_call("bars", symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No OHLCV data from mootdx for {code}")

        # mootdx returns index named 'datetime' AND a column named 'datetime'
        # (plus year/month/day/hour/minute/volume). Drop duplicates before reset.
        df = df.drop(columns=["datetime", "year", "month", "day", "hour", "minute"], errors="ignore")
        df = df.reset_index()  # moves index 'datetime' → column 'datetime'
        rename_map = {
            "datetime": "Date",
            "open": "Open",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        }
        df = df.rename(columns=rename_map)
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df = _normalize_ohlcv_dates(df)
    except Exception as e:
        logger.warning("mootdx OHLCV failed for %s: %s, trying eastmoney/tencent/sina HTTP fallback", code, e)
        # Fallback 1: 东财 push2his（_em_get 节流）
        df = _em_kline_fallback(code)
        if df.empty:
            # Fallback 2: 腾讯 web.ifzq.gtimg.cn（astock-data 同源已验证）
            try:
                df = _tencent_kline_fallback(code)
            except Exception:
                df = pd.DataFrame()
        if df.empty:
            # Fallback 3: Sina direct HTTP API
            try:
                df = _sina_kline_fallback(code)
                if df.empty:
                    raise ValueError(f"No OHLCV data from sina for {code}")
            except Exception:
                raise ValueError(f"No OHLCV data from mootdx/eastmoney/tencent/sina for {code}")

    df, _ = _supplement_stale_ohlcv_with_sina(code, df, curr_date, start_date=None)

    # Cache to disk
    df.to_csv(cache_file, index=False, encoding="utf-8")

    # Filter by curr_date to prevent look-ahead bias
    cutoff = pd.to_datetime(curr_date)
    return df[df["Date"] <= cutoff]


# ===========================================================================
# 9 Vendor Methods (matching interface.py VENDOR_METHODS signatures)
# ===========================================================================


# ---- 1. get_stock_data ----


def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, SH688017)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV stock price data via mootdx."""
    code = _normalize_ticker(symbol)

    data_source = "mootdx (TCP)"
    try:
        df = _mootdx_call("bars", symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No data from mootdx for {code}")

        # Drop duplicate datetime column + extra columns before reset_index
        df = df.drop(
            columns=["datetime", "year", "month", "day", "hour", "minute"],
            errors="ignore",
        )
        df = df.reset_index()  # index 'datetime' → column 'datetime'
        df = df.rename(
            columns={
                "datetime": "Date",
                "open": "Open",
                "close": "Close",
                "high": "High",
                "low": "Low",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        df = _normalize_ohlcv_dates(df)

    except Exception as e:
        logger.warning("mootdx K-line failed for %s: %s, trying eastmoney/tencent/sina HTTP fallback", code, e)
        # Fallback 1: 东财 push2his（_em_get 节流）
        df = _em_kline_fallback(code, start_date, end_date)
        data_source = "eastmoney HTTP (fallback)"
        if df.empty:
            # Fallback 2: 腾讯 web.ifzq.gtimg.cn（astock-data 同源已验证）
            try:
                df = _tencent_kline_fallback(code, start_date, end_date)
                data_source = "tencent HTTP (fallback)"
            except Exception:
                df = pd.DataFrame()
        if df.empty:
            # Fallback 3: Sina direct HTTP API
            try:
                df = _sina_kline_fallback(code, start_date, end_date)
                if df.empty:
                    return "K线数据获取失败：mootdx、东财、腾讯和新浪备用源均不可用，请检查网络连接"
                data_source = "sina HTTP (fallback)"
            except Exception:
                return "K线数据获取失败：mootdx、东财、腾讯和新浪备用源均不可用，请检查网络连接"

    df, supplemented = _supplement_stale_ohlcv_with_sina(code, df, end_date, start_date)
    if supplemented:
        data_source = f"{data_source} + sina HTTP supplement"

    # Filter by date range
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        return (
            f"No data found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    csv_out = df[["Date", "Open", "High", "Low", "Close", "Volume"]].to_csv(
        index=False
    )

    header = f"# Stock data for {code} (A-stock) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {data_source}\n"
    header += (
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )

    return header + csv_out


# ---- 2. get_indicators ----

# Supported technical indicators with descriptions
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD line.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Momentum overbought/oversold indicator (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: 2 std devs above middle.",
    "boll_lb": "Bollinger Lower Band: 2 std devs below middle.",
    "atr": "ATR: Average True Range volatility measure.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index (volume + price momentum).",
}


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[
        str, "technical indicator (e.g. rsi, macd, close_50_sma)"
    ],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicators using stockstats on mootdx OHLCV data."""
    from stockstats import wrap

    code = _normalize_ticker(symbol)

    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    try:
        data = _load_ohlcv_astock(code, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        # Trigger stockstats calculation
        df[indicator]

        # Build date -> value lookup
        ind_dict = {}
        for _, row in df.iterrows():
            d = row["Date"]
            v = row[indicator]
            ind_dict[d] = "N/A" if pd.isna(v) else str(round(float(v), 4))

        # Generate output for look_back window
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        before = curr_dt - relativedelta(days=look_back_days)

        lines = []
        dt = curr_dt
        while dt >= before:
            ds = dt.strftime("%Y-%m-%d")
            val = ind_dict.get(ds, "N/A: Not a trading day (weekend or holiday)")
            lines.append(f"{ds}: {val}")
            dt -= relativedelta(days=1)

        result = (
            f"## {indicator} values for {code} "
            f"from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + _INDICATOR_DESCRIPTIONS.get(indicator, "")
        )
        return result

    except Exception as e:
        return f"Error calculating {indicator} for {code}: {str(e)}"


# ---- 3. get_fundamentals ----


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get company fundamentals from Tencent + mootdx + Eastmoney + 同花顺."""
    code = _normalize_ticker(ticker)

    try:
        lines = []
        # 腾讯行情只有"此刻"的 PE/PB/市值，拿不到历史时点值。复盘历史日期时
        # 必须明说，否则模型会把今天的估值写成分析日当天的事实（未来函数）。
        if _is_historical(curr_date):
            lines.append(_snapshot_notice(curr_date, "估值与行情数据"))

        # --- Tencent: real-time valuation ---
        try:
            tq = _tencent_quote([code])
            if code in tq:
                q = tq[code]
                lines.extend(
                    [
                        f"Name: {q['name']}",
                        f"Price: {q['price']}",
                        f"PE (TTM): {q['pe_ttm']}",
                        f"PE (Dynamic): {q['pe_dynamic']}",
                        f"PB: {q['pb']}",
                        f"Market Cap (100M CNY): {q['mcap_yi']}",
                        f"Float Market Cap (100M CNY): {q['float_mcap_yi']}",
                        f"Turnover Rate: {q['turnover_pct']}%",
                        f"Change: {q['change_pct']}%",
                        f"Limit Up: {q['limit_up']}",
                        f"Limit Down: {q['limit_down']}",
                    ]
                )
        except Exception as e:
            logger.warning("Tencent quote failed for %s: %s", code, e)

        # --- mootdx: financial snapshot (quarterly) ---
        try:
            fin = _mootdx_call("finance", symbol=code)
            if fin is not None and not (
                isinstance(fin, pd.DataFrame) and fin.empty
            ):
                row = fin.iloc[0] if isinstance(fin, pd.DataFrame) else fin
                field_map = {
                    "eps": "EPS (Quarterly)",
                    "bvps": "Book Value Per Share",
                    "roe": "ROE (%)",
                    "profit": "Net Profit",
                    "income": "Revenue",
                    "liutongguben": "Float Shares",
                    "zongguben": "Total Shares",
                }
                idx = row.index if hasattr(row, "index") else []
                for field, label in field_map.items():
                    if field in idx:
                        val = row[field]
                        if val is not None and str(val) != "nan":
                            lines.append(f"{label}: {val}")
        except Exception as e:
            logger.warning("mootdx finance failed for %s: %s", code, e)

        # --- Eastmoney push2: basic stock info (direct HTTP) ---
        try:
            market_code = 1 if code.startswith("6") else 0
            _info_url = "https://push2.eastmoney.com/api/qt/stock/get"
            _info_params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            }
            r = _em_get(_info_url, params=_info_params, timeout=10)
            d = r.json().get("data", {})
            if d:
                if d.get("f127"):
                    lines.append(f"行业: {d['f127']}")
                if d.get("f84"):
                    lines.append(f"总股本: {d['f84']}")
                if d.get("f85"):
                    lines.append(f"流通股本: {d['f85']}")
                if d.get("f116"):
                    lines.append(f"总市值: {d['f116']}")
                if d.get("f117"):
                    lines.append(f"流通市值: {d['f117']}")
                if d.get("f189"):
                    lines.append(f"上市日期: {d['f189']}")
        except Exception as e:
            logger.warning("eastmoney push2 stock info failed for %s: %s", code, e)

        # --- 同花顺 direct HTTP: consensus EPS forecast ---
        try:
            forecast_df = _ths_eps_forecast(code)
            if forecast_df is not None and not forecast_df.empty:
                lines.append("\n--- Consensus EPS Forecast (同花顺) ---")
                eps_by_year = {}
                for _, row in forecast_df.iterrows():
                    year = str(row["年度"])
                    eps_val = row["预测每股收益"]
                    try:
                        mean_eps = float(eps_val)
                    except (TypeError, ValueError):
                        mean_eps = 0.0
                    lines.append(f"FY{year}: EPS={mean_eps}")
                    eps_by_year[year] = mean_eps

                # Forward PE / PEG / PE digestion
                try:
                    tq = _tencent_quote([code])
                    if code in tq:
                        price = tq[code]["price"]
                        years_sorted = sorted(eps_by_year.keys())
                        if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                            eps_cur = eps_by_year[years_sorted[0]]
                            fwd_pe = price / eps_cur
                            lines.append(
                                f"\nForward PE (FY{years_sorted[0]}): "
                                f"{fwd_pe:.1f}x (price={price}, EPS={eps_cur})"
                            )
                            if (
                                len(years_sorted) >= 2
                                and eps_by_year.get(years_sorted[1], 0) > 0
                            ):
                                eps_next = eps_by_year[years_sorted[1]]
                                cagr = eps_next / eps_cur - 1
                                if cagr > 0:
                                    peg = fwd_pe / (cagr * 100)
                                    lines.append(
                                        f"PEG: {peg:.2f} "
                                        f"(EPS CAGR={cagr * 100:.0f}%)"
                                    )
                                    if fwd_pe > 30:
                                        digest = math.log(fwd_pe / 30) / math.log(
                                            1 + cagr
                                        )
                                        lines.append(
                                            f"PE Digestion to 30x: {digest:.1f} years"
                                        )
                                    else:
                                        lines.append("PE already below 30x target")
                                else:
                                    lines.append(
                                        f"EPS declining ({cagr * 100:.0f}%), "
                                        f"PEG not applicable"
                                    )
                except Exception as e:
                    logger.warning("Forward PE calc failed for %s: %s", code, e)
        except Exception as e:
            logger.warning("Consensus EPS forecast failed for %s: %s", code, e)

        if not lines:
            return f"No fundamentals data found for A-stock '{code}'"

        header = f"# Company Fundamentals for {code} (A-stock)\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + "\n".join(lines)

    except Exception as e:
        return f"Error retrieving fundamentals for {code}: {str(e)}"


# ---- 4. get_balance_sheet ----


def _sina_stock_code(code: str) -> str:
    """Pure 6-digit code → sina format (sh688017 / sz000001 / bj832000)."""
    return f"{_get_prefix(code)}{code}"


def _get_financial_report_sina(
    code: str, report_type: str, freq: str, curr_date: str = None,
) -> pd.DataFrame:
    """Shared helper: fetch financial report via Sina direct HTTP API.

    report_type: '资产负债表' | '利润表' | '现金流量表'

    Sina getFinanceReport2022 schema (实测 2026-09):
      result.data = {
        report_count, report_date: [{date_value, date_description, date_type}],
        report_list: {
          "<YYYYMMDD>": {
            rType, rCurrency, publish_date, ...,
            data: [{item_field, item_title, item_value, item_tongbi, ...}, ...]
          }, ...
        }
      }
    即每个报告期是 report_list 的一个 key，其 ``data`` 是该期的科目条目数组。
    输出 DataFrame：行=科目(item_title)，列=各报告期(YYYY-MM-DD)，值=item_value。
    """
    _report_type_map = {
        "资产负债表": "fzb",
        "利润表": "lrb",
        "现金流量表": "llb",
    }
    source_type = _report_type_map.get(report_type, "lrb")

    prefix = "sh" if code.startswith("6") else "sz"
    paper_code = f"{prefix}{code}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": source_type,
        "type": "0",
        "page": "1",
        "num": "20",
    }
    # Sina 偶发 ConnectionReset（实测 zcfzb/llb 会随机被重置）；重试 ≤2 次，间隔递增。
    resp = None
    for attempt in range(2):
        try:
            resp = _requests.get(
                url, params=params, headers={"User-Agent": _UA}, timeout=20
            )
            break
        except (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout) as e:
            if attempt == 1:
                logger.warning("Sina financial report failed for %s: %s", code, e)
                return pd.DataFrame()
            time.sleep(1.0)
    if resp is None:
        return pd.DataFrame()

    d = resp.json()
    data = (d.get("result") or {}).get("data") or {}
    report_list = data.get("report_list") or {}
    if not isinstance(report_list, dict) or not report_list:
        return pd.DataFrame()

    # 报告期 key 为 YYYYMMDD，按日期倒序；先按 curr_date / annual 过滤。
    # 防未来函数：用「披露日 publish_date」与 cutoff 比较（报告期截止日 <= 当前日
    # 不代表已披露，如 2025-12-31 年报次年 4 月才披露）；publish_date 缺失时
    # 保守回退为报告期 + 法定最晚披露滞后（年报 4 个月 / 其余 3 个月）。
    cutoff = pd.to_datetime(curr_date) if curr_date else None
    periods = []
    for key in sorted(report_list.keys(), reverse=True):
        pk = str(key).strip()
        if len(pk) != 8 or not pk.isdigit():
            continue
        period_dt = pd.to_datetime(pk, format="%Y%m%d", errors="coerce")
        if period_dt is pd.NaT:
            continue
        if cutoff is not None:
            entry = report_list.get(pk) or {}
            pub = pd.to_datetime(
                str(entry.get("publish_date") or "").replace("-", ""),
                format="%Y%m%d",
                errors="coerce",
            )
            if pub is pd.NaT:
                lag_months = 4 if pk[4:6] == "12" else 3
                pub = period_dt + pd.DateOffset(months=lag_months)
            if pub > cutoff:
                continue
        if freq.lower() == "annual" and pk[4:6] != "12":
            continue
        periods.append((pk, period_dt))

    # 最多保留 8 个报告期作为列（与原 head(8) 语义一致）。
    periods = periods[:8]
    if not periods:
        return pd.DataFrame()

    # 组装：科目为行，报告期为列。科目顺序以第一个（最新）报告期的 data 顺序为准。
    col_labels = [f"{pk[:4]}-{pk[4:6]}-{pk[6:]}" for pk, _ in periods]
    ordered_titles: list[str] = []
    title_index: dict[str, int] = {}
    table: dict[str, list] = {}  # title -> [values per period]

    for col_i, (pk, _) in enumerate(periods):
        entry = report_list.get(pk) or {}
        items = entry.get("data") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("item_title") or item.get("item_field") or "").strip()
            if not title:
                continue
            if title not in title_index:
                title_index[title] = len(ordered_titles)
                ordered_titles.append(title)
                table[title] = [None] * len(periods)
            table[title][col_i] = item.get("item_value")

    if not ordered_titles:
        return pd.DataFrame()

    df = pd.DataFrame(
        {"科目": ordered_titles}
    )
    for col_i, label in enumerate(col_labels):
        df[label] = [table[t][col_i] for t in ordered_titles]

    return df


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "资产负债表", freq, curr_date)

        if df.empty:
            return f"No balance sheet data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving balance sheet for {code}: {str(e)}"


# ---- 5. get_cashflow ----


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "现金流量表", freq, curr_date)

        if df.empty:
            return f"No cash flow data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Cash Flow for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving cash flow for {code}: {str(e)}"


# ---- 6. get_income_statement ----


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "利润表", freq, curr_date)

        if df.empty:
            return f"No income statement data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Income Statement for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving income statement for {code}: {str(e)}"


# ---- 7. get_news ----


def _fetch_news_eastmoney(code: str, page_size: int = 20) -> list[dict]:
    """Direct East Money search API for individual stock news."""
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_param = {
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    params = {
        "cb": "callback",
        "param": _json.dumps(inner_param, ensure_ascii=False),
        "_": "1",
    }
    headers = {
        "Referer": "https://so.eastmoney.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    resp = _em_get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    text = resp.text
    text = text[text.index("(") + 1 : text.rindex(")")]
    data = _json.loads(text)

    articles: list[dict] = []
    for item in data.get("result", {}).get("cmsArticleWebOld", []):
        articles.append({
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "time": item.get("date", ""),
            "source": item.get("mediaName", "东方财富"),
            "url": item.get("url", ""),
        })
    return articles


def _fetch_news_sina(code: str, page_size: int = 20) -> list[dict]:
    """Sina Finance stock news API (backup source)."""
    prefix = _get_prefix(code)
    url = (
        f"https://vip.stock.finance.sina.com.cn/corp/view/"
        f"vCB_AllNewsStock.php?symbol={prefix}{code}&Page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
    }

    resp = _requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = "gb2312"
    html = resp.text

    articles: list[dict] = []
    rows = _re.findall(
        r"(\d{4}-\d{2}-\d{2})\s*(?:&nbsp;)*(\d{2}:\d{2})\s*(?:&nbsp;)*"
        r"<a[^>]+href='([^']+)'[^>]*>([^<]+)</a>",
        html,
    )
    for date_str, time_str, link, title in rows[:page_size]:
        articles.append({
            "title": title.strip(),
            "content": "",
            "time": f"{date_str} {time_str}",
            "source": "新浪财经",
            "url": link,
        })
    return articles


def get_news(
    ticker: Annotated[str, "A-stock code"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Get stock-specific news via East Money direct API (Sina as fallback)."""
    code = _normalize_ticker(ticker)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    articles: list[dict] = []
    source_label = ""

    try:
        articles = _fetch_news_eastmoney(code)
        source_label = "东方财富"
    except Exception as e:
        logger.warning("East Money news fetch failed for %s: %s", code, e)

    if not articles:
        try:
            articles = _fetch_news_sina(code)
            source_label = "新浪财经"
        except Exception as e:
            logger.warning("Sina news fetch failed for %s: %s", code, e)

    if not articles:
        return f"No news found for A-stock '{code}'"

    news_str = ""
    count = 0
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if pub_dt < start_dt or pub_dt > end_dt:
                continue
        except (ValueError, IndexError):
            pass

        title = art["title"]
        content = art.get("content", "")
        source = art.get("source", source_label)
        link = art.get("url", "")

        news_str += f"### {title} (source: {source})\n"
        if content:
            snippet = content[:300] + "..." if len(content) > 300 else content
            news_str += f"{snippet}\n"
        if link and link != "nan":
            news_str += f"Link: {link}\n"
        news_str += "\n"
        count += 1

    if count == 0:
        return (
            f"No news found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    return (
        f"## {code} (A-stock) News, from {start_date} to {end_date}:\n\n"
        + news_str
    )


# ---- 8. get_global_news ----


def get_global_news(
    curr_date: Annotated[str, "Current date yyyy-mm-dd"],
    look_back_days: Annotated[int, "Days to look back"] = 7,
    limit: Annotated[int, "Max articles"] = 10,
) -> str:
    """Get China/global financial news via direct HTTP (CLS + Eastmoney)."""
    start_dt = datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(
        days=look_back_days
    )
    start_date = start_dt.strftime("%Y-%m-%d")

    all_news: list[dict] = []

    # Source 1: CLS wire (财联社快讯) — direct HTTP
    try:
        cls_url = "https://www.cls.cn/nodeapi/telegraphList"
        cls_params = {"rn": str(limit), "page": "1"}
        cls_headers = {"User-Agent": _UA, "Referer": "https://www.cls.cn/"}
        r_cls = _requests.get(cls_url, params=cls_params, headers=cls_headers, timeout=10)
        d_cls = r_cls.json()
        for item in d_cls.get("data", {}).get("roll_data", []):
            title = item.get("title", "") or item.get("brief", "")
            content = item.get("content", "") or item.get("brief", "")
            ctime = item.get("ctime", "")
            # ctime is unix timestamp
            pub_time = ""
            if ctime:
                try:
                    pub_time = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError, OSError):
                    pub_time = str(ctime)
            all_news.append({
                "title": title,
                "content": content,
                "time": pub_time,
                "source": "CLS Wire",
            })
    except Exception as e:
        logger.warning("CLS news fetch failed: %s", e)

    # Source 2: Eastmoney global (东财7x24资讯) — direct HTTP
    try:
        em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        em_params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(limit),
            "req_trace": str(uuid.uuid4()),
        }
        em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r_em = _em_get(em_url, params=em_params, headers=em_headers, timeout=10)
        d_em = r_em.json()
        for item in d_em.get("data", {}).get("fastNewsList", []):
            title = item.get("title", "")
            summary = item.get("summary", "")[:200]
            pub_time = item.get("showTime", "")
            all_news.append({
                "title": title,
                "content": summary,
                "time": pub_time,
                "source": "Eastmoney Global",
            })
    except Exception as e:
        logger.warning("Eastmoney global news fetch failed: %s", e)

    if not all_news:
        return f"No global news found for {curr_date}"

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[dict] = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    news_str = ""
    for n in unique[:limit]:
        news_str += f"### {n['title']} (source: {n['source']})\n"
        if n.get("content"):
            snippet = (
                n["content"][:300] + "..."
                if len(n["content"]) > 300
                else n["content"]
            )
            news_str += f"{snippet}\n"
        news_str += "\n"

    return (
        f"## China & Global Market News, from {start_date} to {curr_date}:\n\n"
        + news_str
    )


# ---- 9. get_insider_transactions ----


def get_insider_transactions(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Get shareholder/insider activity for an A-stock.

    数据源顺序：**东财 datacenter 优先**（结构化：十大流通股东/十大股东/大股东增减持/
    股东户数）→ 回退 mootdx F10「股东研究」文本（原实现）。

    动因（2026-09-11 排查）：mootdx 走通达信 TCP 7709 协议，本网络下 14 台服务器
    "端口能连上但协议握手/取数被拒"，原实现无回退 → 游资追踪师与解禁监控师各出现
    「内部人交易数据缺失/接口不可用」、「前十大股东完整明细缺失」。datacenter 为 HTTPS，
    与龙虎榜/解禁同源且线上实测可用（RPT_F10_EH_HOLDERS / RPT_F10_EH_FREEHOLDERS /
    RPT_SHARE_HOLDER_INCREASE / RPT_HOLDERNUMLATEST 均返回真实数据）。
    """
    code = _normalize_ticker(ticker)

    try:
        sections = _datacenter_holder_sections(code)
    except Exception as e:  # noqa: BLE001 - 回退链任一环失败都不应让工具整体失败
        logger.warning("datacenter holder fallback failed for %s: %s", code, e)
        sections = []
    if sections:
        header = f"# Shareholder Research for {code} (A-stock)\n"
        header += "# Note: A-stock equivalent of insider transactions\n"
        header += "# Data source: 东财 datacenter（mootdx F10 不可用时的回退）\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        return header + "\n".join(sections)

    try:
        text = _mootdx_call("F10", symbol=code, name="股东研究")

        if not text or not text.strip():
            return f"No insider/shareholder data found for A-stock '{code}'"

        header = f"# Shareholder Research for {code} (A-stock)\n"
        header += "# Note: A-stock equivalent of insider transactions\n"
        header += "# Data source: mootdx F10\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        import re

        sec4_hits = list(re.finditer(r"\r?\n【4\.股东变化】\r?\n", text))
        if sec4_hits:
            sec4_pos = sec4_hits[-1].start()
            before_sec4 = text[:sec4_pos]
            sec4_text = text[sec4_pos:]
            cut_at = 2000
            if len(sec4_text) > cut_at:
                sec4_text = (
                    sec4_text[:cut_at]
                    + "\n\n(... older shareholder history omitted, "
                    f"{len(text) - sec4_pos - cut_at} chars truncated ...)"
                )
            text = before_sec4 + sec4_text

        return header + text

    except Exception as e:
        return f"Error retrieving insider/shareholder data for {code}: {str(e)}"


# ---- 10. get_profit_forecast ----


def get_profit_forecast(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date — 用于判断是否在复盘历史"] = None,
) -> str:
    """Get consensus EPS forecasts with forward valuation (同花顺 direct HTTP)."""
    code = _normalize_ticker(ticker)

    try:
        df = _ths_eps_forecast(code)

        if df is None or df.empty:
            return f"No analyst coverage found for A-stock '{code}'"

        lines = [
            f"# Consensus EPS Forecast for {code} (A-stock)",
            f"# Source: 同花顺 analyst consensus (direct HTTP)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        # 一致预期是"当前"的分析师预测，没有历史时点版本。同上，必须明说。
        if _is_historical(curr_date):
            lines.insert(0, _snapshot_notice(curr_date, "分析师一致预期"))

        eps_by_year = {}
        for _, row in df.iterrows():
            year = str(row["年度"])
            eps_val = row["预测每股收益"]
            try:
                mean_eps = float(eps_val)
            except (TypeError, ValueError):
                mean_eps = 0.0
            lines.append(f"FY{year}: EPS={mean_eps}")
            eps_by_year[year] = mean_eps

        # Forward valuation
        try:
            tq = _tencent_quote([code])
            if code in tq:
                price = tq[code]["price"]
                pe_ttm = tq[code]["pe_ttm"]
                lines.append(f"\nCurrent: price={price}, PE(TTM)={pe_ttm}")

                years_sorted = sorted(eps_by_year.keys())
                if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                    eps_cur = eps_by_year[years_sorted[0]]
                    fwd_pe = price / eps_cur
                    lines.append(
                        f"Forward PE (FY{years_sorted[0]}): {fwd_pe:.1f}x"
                    )
                    if (
                        len(years_sorted) >= 2
                        and eps_by_year.get(years_sorted[1], 0) > 0
                    ):
                        eps_next = eps_by_year[years_sorted[1]]
                        cagr = eps_next / eps_cur - 1
                        if cagr > 0:
                            peg = fwd_pe / (cagr * 100)
                            lines.append(
                                f"PEG: {peg:.2f} (CAGR={cagr * 100:.0f}%)"
                            )
                            if fwd_pe > 30:
                                digest = math.log(fwd_pe / 30) / math.log(
                                    1 + cagr
                                )
                                lines.append(
                                    f"PE Digestion to 30x: {digest:.1f} years"
                                )
                        else:
                            lines.append(
                                f"EPS declining ({cagr * 100:.0f}%), "
                                f"PEG not applicable"
                            )
        except Exception as e:
            logger.warning("Forward PE calc failed for %s: %s", code, e)

        return "\n".join(lines)

    except Exception as e:
        return f"Error retrieving profit forecast for {code}: {str(e)}"


# ---- 11. get_hot_stocks ----


def get_hot_stocks(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty string for today"] = "",
) -> str:
    """Get strong stocks with topic attribution from 同花顺 editorial team.

    Returns stocks that hit limit-up with human-curated reason tags
    explaining WHY they surged (e.g. '算力租赁+AI政务').
    """
    import requests

    if not curr_date or curr_date.strip() == "":
        curr_date = datetime.now().strftime("%Y-%m-%d")

    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{curr_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        if data.get("errocode", 0) != 0:
            return f"同花顺 API error: {data.get('errormsg', 'unknown')}"

        rows = data.get("data") or []
        if not rows:
            return (
                f"No hot stocks data for {curr_date} "
                f"(may be non-trading day or data not yet available)"
            )

        lines = [
            f"# Hot Stocks with Topic Attribution ({curr_date})",
            f"# Source: 同花顺 editorial (human-curated reason tags)",
            f"# Total: {len(rows)} stocks",
            "",
        ]

        from collections import Counter

        all_tags: list[str] = []

        for row in rows:
            code = row.get("code", "")
            name = row.get("name", "")
            reason = row.get("reason", "")
            zhangfu = row.get("zhangfu", "")
            huanshou = row.get("huanshou", "")
            chengjiaoe = row.get("chengjiaoe", "")
            dde = row.get("ddejingliang", "")

            lines.append(
                f"{code} {name}: +{zhangfu}% "
                f"换手{huanshou}% 成交额{chengjiaoe} "
                f"大单净量{dde} | {reason}"
            )

            if reason:
                tags = [t.strip() for t in str(reason).split("+") if t.strip()]
                all_tags.extend(tags)

        if all_tags:
            cnt = Counter(all_tags)
            lines.append(f"\n## Theme Frequency (top 15)")
            for tag, n in cnt.most_common(15):
                lines.append(f"  {tag}: {n} stocks")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching hot stocks for {curr_date}: {str(e)}"


# ---- 12. get_northbound_flow ----


def _northbound_cache_path() -> str:
    """Path to local CSV cache for northbound daily close snapshots."""
    from .config import get_config

    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "northbound_daily.csv")


def _save_northbound_snapshot(
    date_str: str, hgt: float, sgt: float | None
) -> None:
    """Append today's northbound snapshot to the local CSV cache (dedup by date).

    ``sgt`` 允许为 None（上游已停止披露深股通盘中净买入），此时写入 ``nan``
    占位而非 0，避免把「缺数据」伪装成「净流入 0 亿」污染历史均值。

    Atomic replacement via temp file + ``os.replace``: when running deep analyses
    for multiple tickers in parallel, multiple TA subprocesses write to this shared
    cache concurrently, so a non-atomic read-modify-write would race and could leave
    a half-written file behind.
    """
    import csv
    import tempfile

    path = _northbound_cache_path()
    existing: dict[str, tuple[str, str]] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 3:
                    existing[row[0]] = (row[1], row[2])
    sgt_txt = "nan" if sgt is None else f"{float(sgt):.2f}"
    existing[date_str] = (f"{hgt:.2f}", sgt_txt)
    sorted_dates = sorted(existing.keys())
    fd, tmp_path = tempfile.mkstemp(
        prefix=".northbound_", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "hgt", "sgt"])
            for d in sorted_dates:
                writer.writerow([d, existing[d][0], existing[d][1]])
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_northbound_history(n: int = 20) -> list[tuple[str, float, float]]:
    """Load last N days of northbound close data from local cache."""
    import csv

    path = _northbound_cache_path()
    if not os.path.exists(path):
        return []
    rows: list[tuple[str, float, float]] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 3:
                try:
                    rows.append((row[0], float(row[1]), float(row[2])))
                except ValueError:
                    continue
    return rows[-n:]


def get_northbound_flow(
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily data (last 20 trading days)"
    ] = False,
) -> str:
    """Get northbound capital flow (沪深股通) from 同花顺 hsgtApi.

    Realtime: minute-level cumulative net buying for HGT(沪股通) + SGT(深股通).
    History: self-cached daily close snapshots (upstream APIs stopped updating
    northbound history since 2024-08).
    """
    import requests

    hsgt_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }

    lines = [
        f"# Northbound Capital Flow ({curr_date})",
        "# Source: 同花顺 hsgtApi (沪深股通) + local cache",
        "",
    ]

    hgt_close = 0.0
    sgt_close: float | None = None
    got_realtime = False

    try:
        url_rt = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = requests.get(url_rt, headers=hsgt_headers, timeout=10)
        d = r.json()

        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if times:
            lines.append("## Realtime (cumulative net buying, 亿元)")
            n = len(times)
            start_idx = max(0, n - 10)
            for i in range(start_idx, n):
                t = times[i]
                h = hgt[i] if i < len(hgt) else "N/A"
                s = sgt[i] if i < len(sgt) else "N/A"
                lines.append(f"  {t}: HGT={h} SGT={s}")

            hgt_close = float(hgt[-1]) if hgt else 0.0
            # 实测（2026-09-06）dayChart 的 sgt 盘中仅约 35 点、09:44 后停更，
            # 且量级（~380）与 hgt（~-9）口径不同：疑似上游已停止披露深股通盘中
            # 净买入。仅当 sgt 覆盖完整时间序列（len==len(times)）时才采信其收盘
            # 值，否则视为不可用（None），避免把残缺/异口径数据混入 Total 造成
            # 巨额假净流入信号（原 -9.28+379.75=+370.47 亿实为沪股通净流出）。
            sgt_full = bool(sgt) and len(sgt) == len(times)
            sgt_close = float(sgt[-1]) if sgt_full else None
            if sgt_close is None:
                total = hgt_close
                lines.append(
                    f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                    f"SGT(深股通)=N/A(上游盘中停更) "
                    f"Total(仅HGT)={total:.2f}亿"
                )
            else:
                total = hgt_close + sgt_close
                lines.append(
                    f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                    f"SGT(深股通)={sgt_close:.2f}亿 "
                    f"Total={total:.2f}亿"
                )
            if total > 0:
                lines.append("Signal: Net northbound INFLOW (bullish)")
            elif total < 0:
                lines.append("Signal: Net northbound OUTFLOW (bearish)")
            got_realtime = True
        else:
            lines.append("No realtime data (non-trading hours or holiday)")

        if got_realtime:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _save_northbound_snapshot(today_str, hgt_close, sgt_close)

        if include_history:
            history = _load_northbound_history(20)
            if history:
                lines.append("\n## Historical Daily Close (local cache, 亿元)")
                lines.append("Date       | HGT(沪股通) | SGT(深股通) | Total")
                for date, h, s in history:
                    if s != s:  # NaN：深股通缺数据
                        lines.append(f"  {date}: HGT={h:.2f} SGT=N/A Total={h:.2f}")
                    else:
                        lines.append(
                            f"  {date}: HGT={h:.2f} SGT={s:.2f} Total={h + s:.2f}"
                        )
                # 均值口径：深股通缺失（NaN）时仅按 HGT 计，避免 NaN 传染或按 0 低估。
                totals = [h if s != s else h + s for _, h, s in history]
                avg_total = sum(totals) / len(totals)
                lines.append(
                    f"\n{len(history)}-day avg net flow: {avg_total:.2f}亿"
                )
                if got_realtime:
                    today_total = total
                    diff = today_total - avg_total
                    lines.append(
                        f"Today vs avg: {'+' if diff >= 0 else ''}{diff:.2f}亿 "
                        f"({'above' if diff >= 0 else 'below'} average)"
                    )
            else:
                lines.append(
                    "\n## Historical Daily: No cached data yet. "
                    "History accumulates automatically with each call."
                )

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching northbound flow: {str(e)}"


# ---------------------------------------------------------------------------
# Baidu PAE (百度股市通) helpers
# ---------------------------------------------------------------------------

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
        "Gecko/20100101 Firefox/110.0"
    ),
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


# ---- 13. get_concept_blocks ----


def _em_concept_blocks(code: str) -> str:
    """东财 push2 slist(spt=3) 个股所属板块，百度股市通 403 时的降级源。

    spt=3 返回个股所属的全部板块（BKxxxx + 名称 + 当日涨跌幅），行业/概念/
    地域混合、不区分类别（百度按类别分组，此处降级为扁平列表）。经 _em_get
    自动 push2→push2delay 镜像降级。取不到时返回空串，由调用方决定回退文案。
    """
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    url = "https://push2.eastmoney.com/api/qt/slist/get"
    params = {
        "spt": "3",
        "fields": "f12,f14,f3",
        "secid": secid,
        "pi": "0",
        "pz": "100",
        "po": "1",
        "invt": "2",
        "fltt": "2",
    }
    r = _em_get(url, params=params, timeout=12)
    d = r.json()
    diff = (d.get("data") or {}).get("diff") or {}
    blocks = list(diff.values()) if isinstance(diff, dict) else diff
    blocks = [b for b in blocks if isinstance(b, dict) and b.get("f14")]
    if not blocks:
        return ""

    lines = [
        f"# Concept & Sector Blocks for {code} (A-stock)",
        "# Source: 东方财富 push2 (Eastmoney, 百度降级)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 所属板块 (行业/概念/地域)",
    ]
    names: list[str] = []
    for b in blocks:
        name = str(b.get("f14", ""))
        chg = b.get("f3", "")
        try:
            chg_str = f"{float(chg):+.2f}%"
        except (TypeError, ValueError):
            chg_str = str(chg)
        lines.append(f"  {name}: {chg_str}")
        names.append(name)
    if names:
        lines.append(f"\nBlock tags: {' / '.join(names)}")
    return "\n".join(lines)


def get_concept_blocks(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """Get concept/sector/region blocks that a stock belongs to (百度股市通).

    Returns industry classification (申万), concept themes, and region.
    Each block includes current day's change percentage.
    """
    import requests

    code = _normalize_ticker(ticker)

    try:
        url = (
            "https://finance.pae.baidu.com/api/getrelatedblock"
            f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]'
            "&finClientType=pc"
        )
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        d = r.json()

        if str(d.get("ResultCode", -1)) != "0":
            # 百度 PAE 反爬（403/ResultCode 异常）→ 东财 slist 降级
            em = _em_concept_blocks(code)
            if em:
                return em
            return (
                f"Baidu PAE error: ResultCode={d.get('ResultCode')} "
                f"{d.get('ResultMsg', '')}"
            )

        result = d.get("Result", {})
        categories = result.get(code, [])
        if not categories:
            em = _em_concept_blocks(code)
            if em:
                return em
            return f"No concept/block data for {code}"

        lines = [
            f"# Concept & Sector Blocks for {code} (A-stock)",
            f"# Source: 百度股市通 (Baidu PAE)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        concept_names: list[str] = []

        for cat in categories:
            cat_name = cat.get("name", "")
            items = cat.get("list", [])
            if not items:
                continue
            lines.append(f"## {cat_name}")
            for item in items:
                name = item.get("name", "")
                ratio = item.get("ratio", "")
                desc = item.get("describe", "")
                suffix = f" ({desc})" if desc else ""
                lines.append(f"  {name}{suffix}: {ratio}")
                if cat_name == "概念":
                    concept_names.append(name)

        if concept_names:
            lines.append(f"\nConcept tags: {' / '.join(concept_names)}")

        return "\n".join(lines)

    except Exception as e:
        # 百度整体失败（403 非 JSON / 网络异常）→ 东财 slist 降级
        with contextlib.suppress(Exception):
            em = _em_concept_blocks(code)
            if em:
                return em
        return f"Error fetching concept blocks for {code}: {str(e)}"


# ---- 14. get_fund_flow ----


_FUND_FLOW_COLUMNS = ["date", "code", "main", "large", "mid", "small", "super"]


def _fund_flow_cache_path() -> str:
    """本地逐日资金流累积缓存（CSV，位于 data_cache_dir）。

    动因（2026-09-11 排查）：历史资金流原走 push2his，而本网络下 push2 与 push2his
    被**主机级拦截**（实测 RemoteDisconnected，改 UA/Referer 无效），TA 的镜像降级
    push2delay 对历史接口**无能力**（实测 daykline 仅返回当日 1 行、kline 返回 0 行），
    东财 datacenter 亦无对应资金流报表 → 20 日窗口只能靠本地逐日累积（"自给"）。
    """
    from .config import get_config

    cache_dir = get_config().get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "fund_flow_daily.csv")


def _save_fund_flow_snapshot(date_str: str, code: str, values: dict) -> None:
    """按 (date, code) 去重写入本地缓存；原子替换（多票并行深析的并发安全）。

    缺字段写 ``nan`` 占位而不是 0，避免把「没取到」伪装成「净流入 0」。
    """
    import csv
    import tempfile

    path = _fund_flow_cache_path()
    existing: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= len(_FUND_FLOW_COLUMNS):
                    existing[(row[0], row[1])] = row[: len(_FUND_FLOW_COLUMNS)]
    key = (str(date_str)[:10], str(code))
    row = [key[0], key[1]]
    for col in _FUND_FLOW_COLUMNS[2:]:
        try:
            row.append("nan" if values.get(col) is None else f"{float(values[col]):.0f}")
        except (TypeError, ValueError):
            row.append("nan")
    existing[key] = row
    fd, tmp_path = tempfile.mkstemp(
        prefix=".fundflow_", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(_FUND_FLOW_COLUMNS)
            for k in sorted(existing):
                writer.writerow(existing[k])
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_fund_flow_history(code: str, n: int = 20, cutoff: str = "") -> list:
    """读本地累积缓存 → ``[{date, main, large, mid, small, super}]``（升序，最近 n 条）。"""
    import csv

    path = _fund_flow_cache_path()
    if not os.path.exists(path):
        return []
    rows: list = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < len(_FUND_FLOW_COLUMNS) or row[1] != str(code):
                continue
            date_str = row[0][:10]
            if cutoff and date_str > cutoff:
                continue
            item = {"date": date_str}
            for col, raw in zip(_FUND_FLOW_COLUMNS[2:], row[2:]):
                try:
                    item[col] = None if raw == "nan" else float(raw)
                except ValueError:
                    item[col] = None
            rows.append(item)
    rows.sort(key=lambda r: r["date"])
    return rows[-n:]


def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily fund flow (last 20 days)"
    ] = True,
) -> str:
    """Get individual stock fund flow from 东财 push2.

    Realtime: minute-level main/large/medium/small/super order net inflow.
    History: daily net inflow for 20 trading days (push2his).

    V0.2.7: replaced 百度 PAE (fundflow/fundsortlist, offline since 2026-05)
    with 东财 push2 fund flow API.
    """
    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    lines = [
        f"# Fund Flow for {code} (A-stock)",
        f"# Source: 东财 push2 (Eastmoney)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    historical = _is_historical(curr_date)
    if historical:
        # 分钟级资金流只有"今天"的，复盘历史日期时整段都是未来数据，直接不取。
        lines.append(
            f"（分析日期 {curr_date} 早于今天，已略去实时分钟资金流——"
            f"那是今天的盘中数据，不是 {curr_date} 当天的。）\n"
        )

    try:
        # Realtime minute-level fund flow
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        klines = []
        if not historical:
            r = _em_get(url_rt, params=params_rt, timeout=10)
            d = r.json()
            klines = d.get("data", {}).get("klines", [])

        if klines:
            lines.append(
                "## Realtime Minute Flow "
                "(主力/小单/中单/大单/超大单 净流入, 元)"
            )
            for line in klines[-10:]:
                parts = line.split(",")
                if len(parts) >= 6:
                    lines.append(
                        f"  {parts[0]}: "
                        f"主力={float(parts[1])/1e4:.0f}万 "
                        f"大单={float(parts[4])/1e4:.0f}万 "
                        f"超大单={float(parts[5])/1e4:.0f}万"
                    )

            last_parts = klines[-1].split(",")
            if len(last_parts) >= 2:
                main_net = float(last_parts[1])
                lines.append(
                    f"\nClose: 主力净流入={main_net/1e4:.0f}万元"
                )
                if main_net > 0:
                    lines.append(
                        "Signal: Net main force INFLOW (bullish)"
                    )
                elif main_net < 0:
                    lines.append(
                        "Signal: Net main force OUTFLOW (bearish)"
                    )
            if len(last_parts) >= 6:
                # 逐日累积到本地缓存：外部历史接口在本网络不可用（见 _fund_flow_cache_path），
                # 这是 20 日窗口的唯一来源。写失败只告警，不影响本次返回。
                try:
                    _save_fund_flow_snapshot(
                        str(_market_today()),
                        code,
                        {
                            "main": last_parts[1],
                            "small": last_parts[2],
                            "mid": last_parts[3],
                            "large": last_parts[4],
                            "super": last_parts[5],
                        },
                    )
                except Exception as save_err:  # noqa: BLE001
                    logger.warning(
                        "fund flow cache write failed for %s: %s", code, save_err
                    )
        else:
            lines.append(
                "No realtime fund flow (non-trading hours or holiday)"
            )

        # Historical daily fund flow：外部接口（push2his）+ 本地累积缓存合并
        if include_history:
            url_hist = (
                "https://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            # 接口返回的是"从今天回溯 lmt 个交易日"，没有 end_date 参数。复盘一个
            # 较早的日期时，若仍只要 20 天，过滤后会**一行不剩**——把"数据不对"
            # 变成"没有数据"，比不过滤更糟。按分析日与今天的间隔把窗口放大到能
            # 覆盖到那一段（上限 500，够回溯约两年）。
            hist_limit = 20
            if historical:
                gap_days = (_market_today() - datetime.strptime(
                    str(curr_date)[:10], "%Y-%m-%d").date()).days
                # 日历日 → 交易日约 ×0.7，再多留 20 天余量
                hist_limit = min(500, 20 + int(gap_days * 0.7) + 20)
            params_hist = {
                "secid": secid, "lmt": hist_limit, "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }

            cutoff = str(curr_date)[:10] if historical else ""
            external: dict = {}
            try:
                rh = _em_get(url_hist, params=params_hist, timeout=10)
                for k in (rh.json().get("data") or {}).get("klines") or []:
                    parts = k.split(",")
                    if len(parts) >= 6:
                        external[parts[0][:10]] = parts
            except Exception as hist_err:  # noqa: BLE001
                logger.warning(
                    "fund flow history fetch failed for %s: %s", code, hist_err
                )
            if cutoff:
                # 逐行按分析日截断：接口返回"从今天回溯"，在历史日期上直接打印
                # 等于把未来的资金流喂给模型（未来函数）。
                external = {d: v for d, v in external.items() if d <= cutoff}

            # 本地累积缓存：外部历史接口在本网络不可用时的唯一来源；外部行优先。
            local_rows = _load_fund_flow_history(code, n=60, cutoff=cutoff)
            merged: dict = {
                r["date"]: [r["date"], r["main"], r["small"], r["mid"],
                            r["large"], r["super"]]
                for r in local_rows
            }
            merged.update(external)
            series = [merged[d] for d in sorted(merged)][-20:]

            if series:
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(series)} trading days"
                    + (f", 截至 {cutoff}" if cutoff else "")
                    + ")"
                )
                lines.append(
                    "Date | 主力净流入(万) | 大单(万) "
                    "| 中单(万) | 小单(万) | 超大单(万)"
                )
                for parts in series:
                    lines.append(
                        f"  {str(parts[0])[:10]} "
                        f"| main={_fmt_wan(parts[1])} "
                        f"| large={_fmt_wan(parts[4])} "
                        f"| mid={_fmt_wan(parts[3])} "
                        f"| small={_fmt_wan(parts[2])} "
                        f"| super={_fmt_wan(parts[5])}"
                    )
                if len(series) < 20:
                    # 不再让"降级成功但只有 1 行"冒充正常的 20 日窗口（原实现静默
                    # 输出 "last 1 trading days"，读起来像"就只有 1 天数据"）。
                    lines.append(
                        f"\n注意：外部历史接口不可用或未覆盖（本网络下 push2his 被"
                        f"主机级拦截，降级镜像 push2delay 对历史接口无能力，仅当日 1 行）。"
                        f"当前 {len(series)} 天 = 本地累积缓存 {len(local_rows)} 天 + "
                        f"外部 {len(external)} 天；本地缓存逐日累积，将逐步补齐 20 日窗口。"
                        f"趋势判断请以现有天数为限。"
                    )
            elif historical:
                # 说清楚是"这个日期取不到"，而不是让正文里凭空少一段
                lines.append(
                    f"\n## Historical Daily Fund Flow\n"
                    f"（{cutoff} 及之前的资金流未能取到：外部接口只提供从今天回溯的"
                    f"窗口，分析日过早时可能已超出可回溯范围；本地缓存亦无该段记录。）"
                )
            else:
                lines.append(
                    "\n## Historical Daily Fund Flow\n（暂无数据）"
                )

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching fund flow for {code}: {str(e)}"


# ---------------------------------------------------------------------------
# 15. Dragon Tiger Board (龙虎榜)
# ---------------------------------------------------------------------------

def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    """Get dragon-tiger board (龙虎榜) appearances and seat details.

    Args:
        ticker: 6-digit A-share code, e.g. '000858'
        trade_date: YYYY-MM-DD
        look_back_days: how many days back to search (default 30)

    Returns:
        Formatted text with LHB appearances, top buyer/seller seats,
        and institutional activity.
    """
    code = _normalize_ticker(ticker)
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - pd.Timedelta(days=look_back_days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    lines = [f"# 龙虎榜数据 | {code} | {trade_date} (近{look_back_days}日)"]

    # 1. 上榜记录 — eastmoney datacenter direct HTTP
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(
                f"(TRADE_DATE>='{start_date_str}')"
                f"(TRADE_DATE<='{trade_date}')"
                f"(SECURITY_CODE=\"{code}\")"
            ),
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        if not data:
            lines.append(f"\n近{look_back_days}日未上龙虎榜。")
        else:
            lines.append(f"\n## 上榜记录 ({len(data)} 次)")
            lines.append("日期 | 原因 | 净买入(万) | 换手率")
            for row in data:
                net_buy = round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)
                turnover = round(float(row.get("TURNOVERRATE") or 0), 2)
                lines.append(
                    f"  {str(row.get('TRADE_DATE', ''))[:10]} "
                    f"| {row.get('EXPLANATION', '')} "
                    f"| {net_buy:.0f} "
                    f"| {turnover:.2f}%"
                )
    except Exception as e:
        lines.append(f"龙虎榜列表查询失败: {e}")

    # 2. 最近上榜的买卖席位 — eastmoney datacenter direct HTTP
    try:
        if data:
            latest_date = str(data[0].get("TRADE_DATE", ""))[:10]
            lines.append(f"\n## 最近上榜席位明细 ({latest_date})")

            # 买入席位
            buy_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="BUY",
                sort_types="-1",
            )
            if buy_data:
                lines.append("\n### 买入席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in buy_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )

            # 卖出席位
            sell_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="SELL",
                sort_types="-1",
            )
            if sell_data:
                lines.append("\n### 卖出席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in sell_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )
    except Exception:
        pass

    # 3. 机构动向 — 从买卖席位明细筛选机构专用席位 (OPERATEDEPT_CODE="0")
    try:
        inst_buy = 0.0
        inst_sell = 0.0
        for detail, side in [(buy_data, "buy"), (sell_data, "sell")]:
            for row in (detail or []):
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    if side == "buy":
                        inst_buy += (row.get("BUY") or 0)
                    else:
                        inst_sell += (row.get("SELL") or 0)
        if inst_buy > 0 or inst_sell > 0:
            lines.append("\n## 机构动向")
            lines.append(
                f"  机构买入 {inst_buy/1e4:.0f} 万 "
                f"| 卖出 {inst_sell/1e4:.0f} 万 "
                f"| 净额 {(inst_buy - inst_sell)/1e4:.0f} 万"
            )
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 16. Lockup Expiry Calendar (限售解禁日历)
# ---------------------------------------------------------------------------

def get_lockup_expiry(
    ticker: str,
    trade_date: str,
    forward_days: int = 90,
) -> str:
    """Get lockup expiry schedule for a stock.

    Args:
        ticker: 6-digit A-share code
        trade_date: YYYY-MM-DD
        forward_days: how many days forward to check (default 90)

    Returns:
        Formatted text with historical unlock records and upcoming
        expiry calendar with impact metrics.
    """
    code = _normalize_ticker(ticker)
    lines = [f"# 限售解禁日历 | {code} | {trade_date}"]

    # 1. 历史解禁记录 — eastmoney datacenter direct HTTP
    try:
        history_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")",
            page_size=15,
            sort_columns="FREE_DATE",
            sort_types="-1",
        )
        if history_data:
            lines.append(f"\n## 个股解禁记录 (共 {len(history_data)} 批)")
            lines.append("解禁时间 | 类型 | 解禁数量 | 占比")
            for row in history_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| {row.get('FREE_SHARES_NUM', '')} "
                    f"| {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append("\n无历史解禁记录。")
    except Exception as e:
        lines.append(f"个股解禁查询失败: {e}")

    # 2. 未来待解禁 — eastmoney datacenter direct HTTP
    try:
        end_dt = datetime.strptime(trade_date, "%Y-%m-%d") + pd.Timedelta(
            days=forward_days
        )
        end_str = end_dt.strftime("%Y-%m-%d")
        upcoming_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=(
                f"(SECURITY_CODE=\"{code}\")"
                f"(FREE_DATE>='{trade_date}')"
                f"(FREE_DATE<='{end_str}')"
            ),
            page_size=20,
            sort_columns="FREE_DATE",
            sort_types="1",
        )
        if upcoming_data:
            lines.append(f"\n## 未来 {forward_days} 天待解禁")
            for row in upcoming_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| 数量 {row.get('FREE_SHARES_NUM', '')} "
                    f"| 占比 {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append(f"\n未来 {forward_days} 天无待解禁。")
    except Exception as e:
        lines.append(f"解禁日历查询失败: {e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 17. Industry Comparison (行业横向对比)
# ---------------------------------------------------------------------------

def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    """Get industry sector performance comparison.

    Args:
        ticker: 6-digit A-share code (used to identify relevant sector)
        trade_date: YYYY-MM-DD
        top_n: number of top/bottom industries to show (default 20)

    Returns:
        Formatted text with sector performance ranking, highlighting
        the sector the target stock belongs to.
    """
    code = _normalize_ticker(ticker)
    lines = [f"# 行业横向对比 | {code} | {trade_date}"]

    # 东财 push2 行业板块排名 (direct HTTP, replaces 同花顺 which has 401)
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "100",
            "po": "1",
            # po 只定升降方向，排序字段必须由 fid 指定；缺省时服务端按 f12(代码)
            # 返回，导致「排名前 N」与涨跌幅无关。显式按 f3(涨跌幅) 降序。
            "fid": "f3",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f106,f128,f136,f140,f141,f207",
        }
        r = _em_get(url, params=params, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])

        if items:
            lines.append(
                f"\n## 全行业表现 (东财 {len(items)} 个行业)"
            )
            lines.append(
                "排名 | 行业 | 涨跌幅 | 上涨 | 下跌 | 平盘 | 领涨股"
            )
            for i, item in enumerate(items):
                name = item.get("f14", "")
                change_pct = item.get("f3", 0)
                up_count = item.get("f104", 0)
                down_count = item.get("f105", 0)
                flat_count = item.get("f106", 0)
                # f128=领涨股名称、f140=领涨股代码；展示用名称，代码兜底。
                leader = item.get("f128") or item.get("f140") or ""
                lines.append(
                    f"  {i+1}. {name} "
                    f"| {change_pct}% "
                    f"| {up_count} "
                    f"| {down_count} "
                    f"| {flat_count} "
                    f"| {leader}"
                )
                if i >= top_n * 2 - 1:
                    lines.append(f"  ... (showing top/bottom {top_n})")
                    break
        else:
            lines.append("行业数据获取为空。")
    except Exception as e:
        lines.append(f"行业对比查询失败: {e}")

    return "\n".join(lines)
