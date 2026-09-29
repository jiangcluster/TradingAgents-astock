"""提示词证据截断（Trader 与 Portfolio Manager 共用，口径统一）。

为什么独立模块：两个决策节点的证据窗口此前各写各的（Trader 里散着三个硬编码数字，PM 干脆
**不给**原始报告）—— 口径分散正是"终裁看不到原始证据"的成因之一。
统一到此处后：报告类上限走配置（`evidence_clip_chars`），截断一律带**显式标注**
（模型能看出这里被截断，也知道完整内容去哪里找，而不是误以为拿到的是全文）。

0.5.38：Trader 改用本模块（数值不变，仅集中口径与标注）；PM 新增"分析师原始报告"段。
0.5.42（B17 阈值单源）：兜底值**不再在本文件写字面量**，改由 `default_config` 派生——
此前两处同值却各自硬编码，改一处即静默漂移（无任何守卫）。
"""
from __future__ import annotations

from typing import Optional

from tradingagents.default_config import DEFAULT_CONFIG

# 配置缺失时的兜底上限：**单源**取自 `default_config["evidence_clip_chars"]`（B17 阈值单源）
DEFAULT_EVIDENCE_CHARS = int(DEFAULT_CONFIG["evidence_clip_chars"])


def evidence_clip_limit() -> int:
    """当前生效的**单份报告**截断上限（配置项 `evidence_clip_chars`，缺省取模块常量）。

    配置不可用 / 非法一律回落默认：截断上限不该让决策链挂掉。
    """
    try:
        from tradingagents.dataflows.config import get_config

        value = (get_config() or {}).get("evidence_clip_chars")
    except Exception:                       # noqa: BLE001 —— 配置层异常不得中断决策
        value = None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_EVIDENCE_CHARS
    return limit if limit > 0 else DEFAULT_EVIDENCE_CHARS


def clip_evidence(text: str, source: str = "", limit: Optional[int] = None) -> str:
    """截断证据文本；超限时附显式标注（上限 + 完整内容的去向）。

    `limit=None` → 走配置（`evidence_clip_chars`）；传具体值用于辩论史 / 门控结论等
    性质不同、尺度也不同的内容。
    """
    text = (text or "").strip()
    if limit is None:
        limit = evidence_clip_limit()
    if len(text) <= limit:
        return text
    where = f"，完整内容见 {source}" if source else ""
    return text[:limit] + f"\n...（已截断至 {limit} 字{where}）"
