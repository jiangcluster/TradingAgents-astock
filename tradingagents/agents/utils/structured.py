"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, NamedTuple, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# 结构化路径的来源标记。下游靠它区分"模型按 schema 回答"与"我们只是拿到一段自由文本"
# ——自由文本未必带评级标签，若不标注，解析失败会被静默写成 Hold（见 rating.py）。
FORMAT_STRUCTURED = "structured"
FORMAT_FREETEXT = "freetext"                    # provider 不支持结构化输出
FORMAT_FREETEXT_FALLBACK = "freetext-fallback"  # 结构化调用抛异常后退回


class RenderedOutput(NamedTuple):
    """渲染后的 markdown + 它是怎么来的。"""

    text: str
    format: str


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> RenderedOutput:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    Returns the markdown **and** which path produced it (``RenderedOutput.format``)。
    调用方必须把这个来源带上——自由文本不保证含评级标签，丢了来源就无法区分
    "模型说了 Hold" 和 "我们没看懂它说什么"。

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is None:
        response = plain_llm.invoke(prompt)
        return RenderedOutput(response.content, FORMAT_FREETEXT)

    try:
        result = structured_llm.invoke(prompt)
        return RenderedOutput(render(result), FORMAT_STRUCTURED)
    except Exception as exc:
        logger.warning(
            "%s: structured-output invocation failed (%s); retrying once as free text",
            agent_name, exc,
        )

    response = plain_llm.invoke(prompt)
    return RenderedOutput(response.content, FORMAT_FREETEXT_FALLBACK)
