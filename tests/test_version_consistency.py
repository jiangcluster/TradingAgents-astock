"""版本号三处必须一致（codex 第九轮）。

`pyproject.toml` 是权威值，但 `CHANGELOG.md` 的最新条目和 `CLAUDE.md` 的「当前版本」
也各写了一份。这轮就漏了 `CLAUDE.md`——后续 agent 和发版流程读它会拿到旧版本。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml 里找不到 version"
    return m.group(1)


def test_changelog_top_entry_matches_pyproject():
    version = _pyproject_version()
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[([^\]]+)\]", text, re.M)
    assert m, "CHANGELOG.md 里找不到版本条目"
    assert m.group(1) == version, (
        f"CHANGELOG 最新条目是 {m.group(1)}，pyproject 是 {version}"
    )


def test_claude_md_current_version_matches_pyproject():
    version = _pyproject_version()
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*当前版本\*\*[:：]\s*([0-9][0-9.]*)", text)
    assert m, "CLAUDE.md 里找不到「当前版本」"
    assert m.group(1) == version, (
        f"CLAUDE.md 写的是 {m.group(1)}，pyproject 是 {version}"
    )


def test_package_dunder_version_matches_pyproject():
    """`tradingagents.__version__` 是**运行时**的版本来源。

    headless JSON 的 `ta_version` 取自它，下游（深研）缓存据此做版本握手——
    这里落后于 pyproject 的话，下游会误判结论出自哪一版。
    """
    version = _pyproject_version()
    text = (ROOT / "tradingagents" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    assert m, "tradingagents/__init__.py 里找不到 __version__"
    assert m.group(1) == version, (
        f"tradingagents.__version__ 是 {m.group(1)}，pyproject 是 {version}"
    )


def test_headless_docstring_ta_version_matches_pyproject():
    """`cli/headless.py` 的示例 JSON 里也写了一份 `ta_version`（第五处载体，G15 / 0.5.28）。

    此前只锁四处，于是 0.5.27 发版时这处 docstring 停在 0.5.26——下游按示例对齐会拿到过期版本号。
    """
    version = _pyproject_version()
    text = (ROOT / "cli" / "headless.py").read_text(encoding="utf-8")
    m = re.search(r'"ta_version"\s*:\s*"([^"]+)"', text)
    assert m, "cli/headless.py 里找不到 ta_version 示例"
    assert m.group(1) == version, (
        f"headless.py 示例 ta_version 是 {m.group(1)}，pyproject 是 {version}"
    )
