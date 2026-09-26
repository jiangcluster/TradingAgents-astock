"""Unit tests for model-specific structured-output dispatch."""

import pytest
from pydantic import BaseModel

from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.openai_client import MinimaxChatOpenAI


@pytest.mark.unit
def test_deepseek_v4_and_reasoner_reject_tool_choice():
    for model in ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-reasoner"):
        capabilities = get_capabilities(model)
        assert capabilities.supports_tool_choice is False
        assert capabilities.requires_reasoning_content_roundtrip is True


@pytest.mark.unit
def test_minimax_m2_variants_support_tool_choice_and_reasoning_split():
    for model in ("MiniMax-M2", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        capabilities = get_capabilities(model)
        assert capabilities.supports_tool_choice is True
        assert capabilities.supports_json_mode is False
        assert capabilities.supports_reasoning_split is True


@pytest.mark.unit
def test_unknown_model_uses_permissive_defaults():
    capabilities = get_capabilities("some-future-model")
    assert capabilities.supports_tool_choice is True
    assert capabilities.preferred_structured_method == "function_calling"
    assert capabilities.supports_reasoning_split is False


@pytest.mark.unit
def test_future_minimax_family_does_not_inherit_m2_reasoning_split():
    capabilities = get_capabilities("MiniMax-M3")
    assert capabilities.supports_tool_choice is True
    assert capabilities.supports_reasoning_split is False


@pytest.mark.unit
def test_minimax_payload_enables_reasoning_split():
    client = MinimaxChatOpenAI(
        model="MiniMax-M2.7",
        api_key="placeholder",
        base_url="https://api.minimax.chat/v1",
    )
    payload = client._get_request_payload([{"role": "user", "content": "hi"}])
    assert payload.get("reasoning_split") is True


@pytest.mark.unit
def test_minimax_payload_does_not_enable_reasoning_split_for_custom_model():
    client = MinimaxChatOpenAI(
        model="custom-minimax-model",
        api_key="placeholder",
        base_url="https://api.minimax.chat/v1",
    )
    payload = client._get_request_payload([{"role": "user", "content": "hi"}])
    assert "reasoning_split" not in payload


@pytest.mark.unit
def test_minimax_structured_output_keeps_schema_and_tool_choice():
    class _Sample(BaseModel):
        answer: str

    client = MinimaxChatOpenAI(
        model="MiniMax-M2.7",
        api_key="placeholder",
        base_url="https://api.minimax.chat/v1",
    )
    wrapped = client.with_structured_output(_Sample)
    first = wrapped.steps[0] if hasattr(wrapped, "steps") else wrapped
    kwargs = getattr(first, "kwargs", {})

    tool_choice = kwargs.get("tool_choice")
    assert tool_choice == {
        "type": "function",
        "function": {"name": "_Sample"},
    }
    assert any(
        tool.get("function", {}).get("name") == "_Sample"
        for tool in kwargs.get("tools", [])
    )


@pytest.mark.unit
def test_deepseek_v3_family_keeps_permissive_defaults():
    """V3.2 是 catalog 里在售型号，其 tool_choice 行为未实测过，
    不能被 V4 的结论覆盖（原 `^deepseek-v\\d` 会误伤）。"""
    for model in ("deepseek-v3", "deepseek-v3.2", "deepseek-chat"):
        capabilities = get_capabilities(model)
        assert capabilities.supports_tool_choice is True
        assert capabilities.preferred_structured_method == "function_calling"


@pytest.mark.unit
def test_deepseek_v4_family_still_matched_by_pattern():
    for model in ("deepseek-v4", "deepseek-v4.1", "deepseek-v4-turbo"):
        assert get_capabilities(model).supports_tool_choice is False


@pytest.mark.unit
def test_explicit_tool_choice_is_dropped_for_unsupported_model():
    """能力表声明「不支持 tool_choice」就必须真正生效。
    原实现用 setdefault，调用方显式传入时会被保留，API 调用照样失败。"""
    from unittest.mock import patch
    from langchain_openai import ChatOpenAI
    from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI

    client = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="x")

    class _Schema(BaseModel):
        value: str

    # 必须 patch 到 ChatOpenAI（再上一层）——patch NormalizedChatOpenAI 会把
    # 待测实现本身替换掉，测试就永远绿。
    with patch.object(ChatOpenAI, "with_structured_output", return_value="ok") as parent:
        client.with_structured_output(_Schema, tool_choice="required")

    assert parent.call_args.kwargs["tool_choice"] is None


@pytest.mark.unit
def test_optional_tool_call_returning_none_still_falls_back_to_free_text():
    """tool_choice=None 让 schema 工具变成可选：模型若返回纯文本，
    LangChain 解析器给出 None。此时必须退回自由文本，而不是让节点失败。

    这一条锁住 PR #83 的行为边界——最坏情况与 PR 之前等价（都走自由文本），
    不存在「拿不到结构化就崩」的回归。
    """
    from unittest.mock import MagicMock
    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.return_value = None          # 模型没调工具

    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="free text fallback")

    def render(obj):                                # 真实 render 会对 None 抛 AttributeError
        return f"**Action**: {obj.action}"

    out = invoke_structured_or_freetext(structured, plain, "p", render, "Trader")

    # 返回值现在是 (文本, 来源)：来源必须标成 freetext-fallback，否则调用方无法
    # 区分"模型说了 Hold"与"结构化没拿到、我们退回了自由文本"。
    assert out.text == "free text fallback"
    assert out.format == "freetext-fallback"
    plain.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# 0.5.33：三级通道（tool-calling → json_mode → 自由文本）
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_json_mode_ladder_used_before_free_text():
    """tool-calling 抛异常 → 走 json_mode（而不是直接自由文本）。

    推理档模型（DeepSeek `deepseek-flash`）会拒绝 `tool_choice`：若没有中间这一级，
    每次调用都落到自由文本 —— 评级标签消失（`rating_source` 由 `label` 退化为 `bare`），
    schema 必填字段与"不得给价位"等约束只剩提示词兜底。
    """
    from unittest.mock import MagicMock
    from tradingagents.agents.schemas import TraderProposal
    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("Thinking mode does not support this tool_choice")
    json_llm = MagicMock()
    json_llm.invoke.return_value = TraderProposal(action="Hold", reasoning="r")
    plain = MagicMock()

    out = invoke_structured_or_freetext(
        structured, plain, "p", lambda p: f"**Action**: {p.action.value}", "Trader",
        json_structured=json_llm, schema=TraderProposal,
    )

    assert out.format == "structured-json"
    assert out.text == "**Action**: Hold"
    plain.invoke.assert_not_called()            # 没有落到自由文本
    sent = json_llm.invoke.call_args.args[0]
    assert "json" in sent.lower()               # API 要求提示词含 json 字样
    assert "action" in sent                     # 且须自带字段说明（langchain 不注入 schema）


@pytest.mark.unit
def test_json_mode_ladder_falls_back_when_both_channels_fail():
    from unittest.mock import MagicMock
    from tradingagents.agents.schemas import TraderProposal
    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("tool_choice rejected")
    json_llm = MagicMock()
    json_llm.invoke.side_effect = ValueError("bad json")
    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="free text")

    out = invoke_structured_or_freetext(
        structured, plain, "p", lambda p: "x", "Trader",
        json_structured=json_llm, schema=TraderProposal,
    )

    assert out.format == "freetext-fallback" and out.text == "free text"


@pytest.mark.unit
def test_json_mode_prompt_appends_schema_for_message_lists():
    """Trader 用的是消息列表：hint 必须落到最后一条消息上。"""
    from tradingagents.agents.schemas import TraderProposal
    from tradingagents.agents.utils.structured import json_mode_prompt

    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]

    out = json_mode_prompt(msgs, TraderProposal)

    assert out[0]["content"] == "sys"           # 其他消息不动
    assert out[1]["content"].startswith("u")
    assert "JSON" in out[1]["content"] and "action" in out[1]["content"]
