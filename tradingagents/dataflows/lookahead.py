"""分析日注入 —— look-ahead 守卫的**单一事实源**。

为什么单独一个模块：注入方是图运行层（`graph/trading_graph.py`），消费方是数据层
（`dataflows/a_stock.py`）。两边都 import 本模块即可，避免图运行层反向依赖具体 vendor
（`a_stock` 带 pandas / mootdx / requests 等重依赖）。

**约定：未注入 → 不钳制**（保持既有行为；离线调用与单测不受影响）。

0.5.37 动因：`get_stock_data` 的 `start_date` / `end_date` 全部由 LLM 填写，数据层此前把它
当作权威右边界 —— 模型若给出分析日之后的 `end_date`（或误用"今天"），未来 K 线会直接
进报告。分析日由图运行期注入后，数据层可对其**硬截断**并在输出头部显式提示。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional, Tuple

_ANALYSIS_DATE: ContextVar[Optional[str]] = ContextVar("analysis_date", default=None)


def set_analysis_date(date_str) -> None:
    """注入当前分析日（`YYYY-MM-DD`）；传 `None` / 空串表示清除。"""
    _ANALYSIS_DATE.set(str(date_str).strip() if date_str else None)


def get_analysis_date() -> Optional[str]:
    """当前上下文的分析日；未注入 → `None`。"""
    return _ANALYSIS_DATE.get()


def clamp_end_date(end_date: str) -> Tuple[str, str]:
    """把 `end_date` 钳到分析日以内 → `(生效日期, 提示行或 "")`。

    - 未注入分析日 → 原样返回（不干预，保持既有行为）；
    - `end_date` 缺失 → 原样返回；
    - `end_date` 晚于分析日 → 返回分析日 + 一行显式提示（模型据此知道窗口被截断，
      而不是误以为覆盖到了请求的 `end_date`）。
    """
    analysis = get_analysis_date()
    if analysis and end_date and str(end_date) > analysis:
        return analysis, (
            f"# NOTE: requested end_date {end_date} clamped to analysis date "
            f"{analysis} (look-ahead guard)\n"
        )
    return end_date, ""
