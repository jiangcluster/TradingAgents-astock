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

import json
import logging
from typing import Any, Callable, NamedTuple, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# 结构化路径的来源标记。下游靠它区分"模型按 schema 回答"与"我们只是拿到一段自由文本"
# ——自由文本未必带评级标签，若不标注，解析失败会被静默写成 Hold（见 rating.py）。
FORMAT_STRUCTURED = "structured"                  # tool-calling（默认通道）
# 0.5.33：json_mode 通道（`response_format=json_object`）。DeepSeek 官方 `deepseek-flash`
# 是**推理模型**，会拒绝 tool_choice（400 "Thinking mode does not support this tool_choice"）
# ⇒ tool-calling 通道整条不可用；json_mode 仍可用，故作为**第二条结构化通道**保留
# （仍失败才退回自由文本，并如实标注来源）。
FORMAT_STRUCTURED_JSON = "structured-json"
FORMAT_FREETEXT = "freetext"                    # provider 不支持结构化输出
FORMAT_FREETEXT_FALLBACK = "freetext-fallback"  # 两条结构化通道都失败后退回


class RenderedOutput(NamedTuple):
    """渲染后的 markdown + 它是怎么来的。"""

    text: str
    format: str


def json_mode_prompt(prompt: Any, schema: type[BaseModel]) -> Any:
    """给提示词补「只输出 JSON」+ 字段 schema（json_mode 通道专用）。

    两个硬约束（实测 DeepSeek 官方 `deepseek-flash`，2026-09-26）：

    1. `response_format=json_object` **要求提示词里出现 `json` 一词**，否则直接 400
       （`Prompt must contain the word 'json' in some form…`）；
    2. langchain 的 json_mode **不会**把 schema 注入提示词 —— 不写字段说明时模型只回
       `{"rating": …}`，缺字段会 `ValidationError: executive_summary Field required`。

    故这里同时补「JSON-only 指令」与 `model_json_schema()`。支持 str 与消息列表两种
    prompt 形态（Trader 用的是消息列表）。
    """
    try:
        spec = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    except Exception:                                # pragma: no cover - schema 异常时退化
        spec = ""
    hint = ("\n\n请只输出一个 JSON 对象（不要 markdown 代码块、不要任何额外文字），"
            "其 JSON 字段与类型必须与下列 JSON Schema 一致：\n" + spec)
    if isinstance(prompt, str):
        return prompt + hint
    if isinstance(prompt, list) and prompt:
        msgs = [dict(m) if isinstance(m, dict) else m for m in prompt]
        last = msgs[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), str):
            last = dict(last)
            last["content"] = last["content"] + hint
            msgs[-1] = last
            return msgs
    return prompt


def bind_structured(llm: Any, schema: type[T], agent_name: str,
                    json_mode: bool = False) -> Optional[Any]:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    `json_mode=True`（0.5.33）：绑定 `response_format=json_object` 通道，供
    `invoke_structured_or_freetext` 在 tool-calling 通道被服务端拒绝时降级使用。

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        if json_mode:
            return llm.with_structured_output(schema, method="json_mode")
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s; json_mode=%s); "
            "falling back to free-text generation",
            agent_name, exc, json_mode,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
    json_structured: Optional[Any] = None,
    schema: type[BaseModel] = None,
) -> RenderedOutput:
    """三级通道：tool-calling → json_mode → 自由文本；返回 markdown 与**实际通道**。

    0.5.33 起新增中间一级：DeepSeek 官方推理档（`deepseek-flash`）会拒绝 `tool_choice`
    （400 "Thinking mode does not support this tool_choice"），**整条 tool-calling 通道不可用**；
    `json_mode`（`response_format=json_object`）仍可用，但要求提示词含 `json` 且需自带
    字段说明（见 `json_mode_prompt`）。缺了这一级，每次调用都会静默落到自由文本：
    评级标签消失（`rating_source` 由 `label` 变 `bare`）、schema 里的必填字段与
    "不得给价位"等约束只剩提示词兜底。

    Returns the markdown **and** which path produced it (``RenderedOutput.format``)。
    调用方必须把这个来源带上——自由文本不保证含评级标签，丢了来源就无法区分
    "模型说了 Hold" 和 "我们没看懂它说什么"。

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.

    `schema`：json_mode 通道要求提示词自带字段说明，调用方须传本 agent 的 schema
    （漏传时只会补上「只输出 JSON」——API 不会 400，但缺字段会触发 ValidationError
    进而落到自由文本）。
    """
    if structured_llm is None:
        # provider 完全不支持 tool-calling：仍先试 json_mode（若已绑定）
        if json_structured is not None:
            try:
                return RenderedOutput(
                    render(json_structured.invoke(json_mode_prompt(prompt, schema))),
                    FORMAT_STRUCTURED_JSON,
                )
            except Exception as exc:                  # noqa: BLE001 - 降级链
                logger.warning("%s: json_mode structured output failed (%s)", agent_name, exc)
        response = plain_llm.invoke(prompt)
        return RenderedOutput(response.content, FORMAT_FREETEXT)

    try:
        result = structured_llm.invoke(prompt)
        return RenderedOutput(render(result), FORMAT_STRUCTURED)
    except Exception as exc:
        logger.warning(
            "%s: structured-output invocation failed (%s); trying json_mode before free text",
            agent_name, exc,
        )

    if json_structured is not None:
        try:
            return RenderedOutput(
                render(json_structured.invoke(json_mode_prompt(prompt, schema))),
                FORMAT_STRUCTURED_JSON,
            )
        except Exception as exc:                      # noqa: BLE001 - 降级链
            logger.warning("%s: json_mode structured output failed (%s)", agent_name, exc)

    response = plain_llm.invoke(prompt)
    return RenderedOutput(response.content, FORMAT_FREETEXT_FALLBACK)
