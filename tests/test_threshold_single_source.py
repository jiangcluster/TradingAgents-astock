"""B17 阈值单源（TA 侧）。

规则（见 `review/审查清单.md` §4 台账 B17）：**同一语义的阈值全仓只能有一处字面量**，
其余位置必须从该来源派生或注入；独立尺度须在下面逐条写明理由（不接受无理由豁免）。

本守卫锁两组：
- `evidence_clip_chars`（报告证据截断上限）：唯一来源 `default_config`，`prompt_clip` 由它派生
  ——0.5.38 曾把该值同时写在两处（同值但无守卫，改一处即静默漂移）；
- `_QUALITY_SUMMARY_CHARS`（门控结论截断）：自带尺度，仅 `trader.py` 使用。

判据基于 **AST 的代码常量**（不看注释/文档字符串——文档里叙述历史数值不算阈值副本）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tradingagents.agents.utils import prompt_clip
from tradingagents.default_config import DEFAULT_CONFIG

PKG = Path(__file__).resolve().parent.parent / "tradingagents"

# 语义组 → (代码常量值, 允许出现该常量的文件名集合, 理由)
GROUPS = {
    "报告证据截断上限（evidence_clip_chars）": (
        1500, {"default_config.py"},
        "唯一来源：default_config；prompt_clip 由它派生（本文件不得再写字面量）",
    ),
    "门控结论截断（_QUALITY_SUMMARY_CHARS）": (
        1200, {"trader.py"},
        "与报告截断性质不同、尺度独立，仅 trader 使用（单处即单源）",
    ),
}


def _files_with_int(value: int) -> set:
    found = set()
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and not isinstance(node.value, bool) \
                    and node.value == value:
                found.add(path.name)
                break
    return found


def test_declared_thresholds_are_single_source():
    for label, (value, allowed, reason) in GROUPS.items():
        found = _files_with_int(value)
        assert found == allowed, (
            f"{label} 的常量 {value} 出现在 {sorted(found)}，应只在 {sorted(allowed)} —— {reason}"
        )


def test_evidence_clip_derives_from_default_config():
    assert prompt_clip.DEFAULT_EVIDENCE_CHARS == DEFAULT_CONFIG["evidence_clip_chars"]
    assert prompt_clip.evidence_clip_limit() == DEFAULT_CONFIG["evidence_clip_chars"]


def test_detector_is_not_vacuous():
    """反向对照：凭空造一个数字必须被判为"不存在于任何文件"。"""
    assert _files_with_int(987654321) == set()
