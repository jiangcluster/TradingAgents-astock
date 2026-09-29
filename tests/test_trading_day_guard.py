"""B16 交易日一致性（TA 侧）。

规则（见 `review/审查清单.md` §4 台账 B16）：非交易日（周末）既不能**写**（写进去等于伪造样本、
并污染"连日同值"停更判定基线），也不能在**读**历史时留在序列里（无行情含义，污染均值口径）。
行为用例已在 `test_northbound_cache.py`（0.5.39/0.5.40）；本守卫锁**接线**——防止日后重构
把判定函数留下、却不再被写/读路径调用（"函数孤岛"式静默退化）。

纯源码 AST + 纯函数行为，不触网。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tradingagents.dataflows import a_stock

SRC = Path(a_stock.__file__).read_text(encoding="utf-8")


def _func_source(name: str) -> str:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError(f"a_stock.py 中找不到函数 {name}（守卫不得空转）")


def test_is_weekend_date_three_states():
    assert a_stock._is_weekend_date("2026-09-19") is True   # 周六
    assert a_stock._is_weekend_date("2026-09-20") is True   # 周日
    assert a_stock._is_weekend_date("2026-09-18") is False  # 周五
    # 非法/空值必须**不误伤**（返回 False 而不是抛错）
    for bad in ("", None, "bad", "2026-13-45"):
        assert a_stock._is_weekend_date(bad) is False


def test_write_side_skips_non_trading_day():
    """写侧：`get_northbound_flow` 内必须有非交易日（weekday >= 5）跳过写盘的分支。"""
    write_side = _func_source("get_northbound_flow")
    assert "weekday() >= 5" in write_side, "写侧丢失非交易日门（会伪造样本、污染停更基线）"


def test_read_side_skips_weekend_rows():
    """读侧：历史序列加载必须过滤周末脏行。"""
    read_side = _func_source("_load_northbound_history")
    assert "_is_weekend_date(" in read_side, "读侧未过滤周末行（污染连日同值判定与均值口径）"


def test_market_today_is_used_for_empty_date():
    """市场时区口径（0.5.35）：空日期不得回落主机本地日期。"""
    assert "def _market_today" in SRC
    assert "_market_today()" in SRC
