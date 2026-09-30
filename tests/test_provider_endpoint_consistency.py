"""CLI 与客户端兜底必须指向同一个 provider 端点（#113）。

同一批 provider 的 base URL 写在两处：`cli/utils.py::select_llm_provider()` 的
`PROVIDERS` 列表（CLI 交互用）与 `llm_clients/openai_client.py::_PROVIDER_CONFIG`
（客户端兜底，Web 侧栏 Base URL 留空时生效）。两站都能用同一把 key、返回同一份模型
列表，所以**不报错**，只是从 CLI 跑与从 Web 跑静默走不同网络路径。

**扫源码而不 import**：`cli/utils.py` 顶层 `import questionary`（→ prompt_toolkit），
缺该可选依赖时 import 会失败；用 `pytest.importorskip` 跳过会让守卫在缺依赖环境里
静默失效（本仓已有"守卫被 importorskip 连带跳过"的先例）。故只读文件文本。

反面教训即本条守卫的由来：这两行自 v0.2.4 引入起对 glm/qwen 就没对齐，且**零测试
覆盖**，分裂了多个版本无人发现，直到源仓库 922db59(#113) 才修。
"""
from __future__ import annotations

import re
from pathlib import Path

from tradingagents.llm_clients.openai_client import _PROVIDER_CONFIG

ROOT = Path(__file__).resolve().parent.parent
_CLI_UTILS = ROOT / "cli" / "utils.py"

# ("Display", "provider_key", "https://…") 三元组；base_url 为 None 的行不匹配（None 无引号）。
_TUPLE_RE = re.compile(r'\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')

# 两侧都带 base URL、因而必须逐字一致的 provider。
# 不含 minimax（只有客户端兜底有，CLI 列表里没有）；openai/anthropic 只在 CLI 列表里；
# azure/google/openai_compatible 的 CLI base_url 为 None（运行时再问）。
_MUST_AGREE = {"deepseek", "glm", "ollama", "openrouter", "qwen", "xai"}

# 面向国内用户的 provider，兜底端点不得是海外站（#113 的具体取向）。
# 本仓 README 中英文都让用户去 open.bigmodel.cn 申请 key。
_OVERSEAS_MARKERS = {"glm": "api.z.ai", "qwen": "dashscope-intl"}


def _cli_endpoints(src: str) -> dict[str, str]:
    """从 cli/utils.py 源码文本抽出 {provider_key: base_url}（base_url 为 None 的行跳过）。"""
    return dict(_TUPLE_RE.findall(src))


def _mismatches(cli: dict[str, str], cfg: dict) -> dict[str, tuple[str, str]]:
    """两侧共有、但 base URL 不一致的 provider → {key: (CLI 值, 兜底值)}。"""
    return {
        key: (cli[key], cfg[key][0])
        for key in (set(cli) & set(cfg))
        if cli[key] != cfg[key][0]
    }


def test_cli_and_client_fallback_agree_on_endpoints():
    """两处都定义了的 provider，base URL 必须逐字相同。"""
    cli = _cli_endpoints(_CLI_UTILS.read_text(encoding="utf-8"))
    assert cli, "没从 cli/utils.py 解析出任何 provider，正则或源码结构变了"

    shared = set(cli) & set(_PROVIDER_CONFIG)
    # 必须**恰好相等**而不是"非空"：只断言非空的话，某一侧少写一个 provider（或正则漏
    # 解析一行）会让它悄悄退出比对范围、测试照样绿 —— 那正是本条要防的失效模式。
    assert shared == _MUST_AGREE, (
        f"应当逐字比对的 provider 集合变了：实际 {sorted(shared)}，预期 {sorted(_MUST_AGREE)}。"
        "若是有意增删 provider，请同步改 _MUST_AGREE；否则是某一侧漏写或解析失效。"
    )

    mismatches = _mismatches(cli, _PROVIDER_CONFIG)
    assert not mismatches, (
        "CLI 与客户端兜底对同一 provider 给了不同端点，用户换个入口就换站点：\n"
        + "\n".join(f"  {k}: CLI={v[0]!r} 兜底={v[1]!r}" for k, v in mismatches.items())
    )


def test_domestic_providers_point_to_domestic_sites():
    """glm / qwen 面向国内用户，兜底端点不能是海外站（#113 的具体取向）。"""
    for provider, marker in _OVERSEAS_MARKERS.items():
        url = _PROVIDER_CONFIG[provider][0]
        assert marker not in url, (
            f"{provider} 的兜底端点回到了海外站 {url}；"
            "若确要切换，请同时改 cli/utils.py 与 README 并标 breaking"
        )


def test_guard_catches_fabricated_divergence():
    """反向对照：伪造一个海外站 qwen 端点，守卫必须判出不一致（不得空转）。"""
    faithful = _cli_endpoints(_CLI_UTILS.read_text(encoding="utf-8"))
    assert _mismatches(faithful, _PROVIDER_CONFIG) == {}, "现状应当是一致的"

    tampered = dict(faithful, qwen="https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    assert _mismatches(tampered, _PROVIDER_CONFIG) == {
        "qwen": (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    }, "提取器/比对器退化：伪造的端点不一致未被判出"
