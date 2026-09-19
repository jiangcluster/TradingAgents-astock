"""Extract the 5-tier portfolio rating from the Portfolio Manager's decision.

The Portfolio Manager produces a typed ``PortfolioDecision`` via structured
output and renders it to markdown that always carries a ``**Rating**: X``
header (see :func:`tradingagents.agents.schemas.render_pm_decision`).  The
deterministic heuristic in :mod:`tradingagents.agents.utils.rating` is more
than sufficient to extract that rating; no extra LLM call is needed.

This module exists for backwards compatibility with callers that expect a
``SignalProcessor.process_signal(text)`` interface.
"""

from __future__ import annotations

from typing import Any, Tuple

from tradingagents.agents.utils.rating import parse_rating_with_source


class SignalProcessor:
    """Read the 5-tier rating out of a Portfolio Manager decision."""

    def __init__(self, quick_thinking_llm: Any = None):
        # The LLM argument is accepted for backwards compatibility but no
        # longer used: the PM's structured output guarantees the rating is
        # parseable from the rendered markdown without a second LLM call.
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """Return one of Buy / Overweight / Hold / Underweight / Sell."""
        return self.process_signal_detail(full_signal)[0]

    def process_signal_detail(self, full_signal) -> Tuple[str, str]:
        """同上，并额外返回评级来源（label / bare / fallback）。

        ``fallback`` 意味着**没有任何评级依据**，返回值只是默认的 Hold —— 下游
        必须能把它和"模型真的建议持有"分开，否则一次解析失败就会被写成看多/看空的
        中性结论，且报告里看不出来。
        """
        return parse_rating_with_source(full_signal)
