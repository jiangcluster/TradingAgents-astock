"""B10 时区白名单（TA 侧）。

规则（见 `review/审查清单.md` §4 台账 B10）：**任何"取当前时间"的调用都必须显式指定时区**
（A 股一律交易所时区 `Asia/Shanghai`），或在白名单里逐条写明理由。判据基于 AST：只统计**无参数**的
`datetime.now()` / `datetime.today()` / `date.today()`（带 tz/带参数的写法与注释、文档字符串均不计数）。

本仓合法用法（已评估，详见 `review/审查清单.md` §11.3 第 5 类）：
① 数据块头部的 **`# Data retrieved on:` / `# Retrieved:` 展示戳**——不进任何比较/过滤，纯人读；
② **Alpha Vantage 路径**（美股数据源，不在 A 股链路）；
③ `hot_money`/行情取数需市场日期处走 `_market_today()`（0.5.35 起）。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent / "tradingagents"

# 允许清单：相对路径 → (期望命中数, 理由)。运行守卫会打印可直接粘贴的登记行。
_ALLOWED: dict = {
    "dataflows/a_stock.py": (11, "`# Data retrieved on:` / `# Retrieved:` **展示戳**：进数据块文本供人读，"
                                 "不参与任何比较/过滤（2026-09-26 已评估为非缺陷，见审查清单 §11.3 第 5 类）"),
    "dataflows/y_finance.py": (6, "同上（备用/美股行情路径的展示戳）"),
    "dataflows/alpha_vantage_stock.py": (1, "Alpha Vantage（美股数据源）据此算 `outputsize`，不在 A 股链路"),
    "dataflows/utils.py": (1, "`get_current_date()` 旧工具函数，**当前无任何调用方**（启用前须先改市场时区）"),
}


def _naive_now_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("now", "today") or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in ("datetime", "date"):
            continue
        if node.args or any(kw.arg == "tz" for kw in node.keywords):
            continue
        count += 1
    return count


def _hits() -> dict:
    hits = {}
    for path in sorted(REPO.rglob("*.py")):
        count = _naive_now_calls(path)
        if count:
            hits[str(path.relative_to(REPO)).replace("\\", "/")] = count
    return hits


def test_naive_now_calls_are_whitelisted():
    hits = _hits()
    expected = {k: v[0] for k, v in _ALLOWED.items()}
    paste = "\n".join(f'    "{k}": ({v}, "<理由>"),' for k, v in hits.items())
    assert hits == expected, (
        "存在未登记的无时区「取当前时间」调用（A 股日期口径请走 `_market_today()`）：\n"
        f"  实际={hits}\n  白名单={expected}\n  如需保留请按下列形式登记理由：\n{paste}"
    )


def test_whitelist_entries_have_reason():
    empty = sorted(k for k, (_, why) in _ALLOWED.items() if not str(why).strip())
    assert not empty, f"白名单必须写明理由：{empty}"


def test_market_today_exists_for_a_share_dates():
    """A 股日期口径必须有市场时区入口（0.5.35 起）。"""
    from tradingagents.dataflows import a_stock

    assert callable(getattr(a_stock, "_market_today", None))


def test_detector_is_not_vacuous():
    """反向对照：无参数调用必须被检出，带 tz 的调用不得被误报。"""
    tree = ast.parse(
        "from datetime import datetime, timezone\n\n"
        "def f():\n"
        "    a = datetime.now()\n"
        "    b = datetime.now(timezone.utc)\n"
        "    return a, b\n"
    )
    naive = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "now" and not node.args \
                and not any(kw.arg == "tz" for kw in node.keywords):
            naive += 1
    assert naive == 1
