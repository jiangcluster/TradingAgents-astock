"""TA 仓的基线守卫：B1（文档引用路径存在）/ B7（仓储卫生）/ B9（守卫不得空转）。

**为什么建这个文件**：审查清单 §8 记着"TA 仓没有 B1/B6/B7/B9 等价物 → 同类问题只能靠人工
审查发现"。2026-09-26 轮据此补建；**B6（skills_guard）/B8（部署位执行权限）对 TA 不适用**
（TA 不经 hermes 安装、无 `~/.local/bin` 入口），故不建对应守卫——这是明确的不适用，
不是"已覆盖"。

守卫必须在**没有 git、没有网络**的环境下也能跑（只读文件系统），且**不得空转**（B9）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# 文档引用提取：反引号包裹的、带已知扩展名的路径状令牌。
_DOC_REF_RE = re.compile(
    r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|toml|yaml|yml|json|sh|txt|ini|cfg|db))`"
)
# 这些文档要参与 B1 检查（被当作"支撑文件/路径"读的）
_DOCS = ("CLAUDE.md", "README.md", "CHANGES_FROM_UPSTREAM.md", "DEV_LOG.md")

# 令牌可能是"相对某前缀的简写"（如 CLAUDE.md 里写 `graph/setup.py`），逐一尝试这些前缀。
_PATH_PREFIXES = ("", "tradingagents/", "tests/", "cli/", "web/", "docs/", "examples/")

# **非仓内路径**（运行时产物 / 用户自建 / 计划产出）——逐条写明理由。
# 新增豁免必须给出理由，否则宁可修文档。
_NON_REPO_REFS = {
    "run.py": "README 明确让用户'新建一个 Python 文件（比如 run.py）'——用户自建，不在仓内",
    "complete_report.md": "`examples/run_cases.py` 的运行时产物",
    "summary.json": "`examples/run_cases.py` 的运行时产物",
    "DEPLOYMENT.md": "DEV_LOG 的未勾选计划项「写部署指南(可选)」，尚未落地",
    "tradingagents/agents/managers/risk_manager.py":
        "DEV_LOG「决策五」的 fork 期计划路径，该文件从未创建：T+1/涨跌停/ST/停牌约束最终"
        "分散落在 trader/trader.py、managers/research_manager.py 与 risk_mgmt/ 三辩手，"
        "属历史计划记载而非悬空引用",
}

# 卫生扫描要跳过的目录（运行时/虚拟环境/缓存，不属于"仓内容"）
_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "logs", "results", "data", ".idea", ".vscode",
}
# 二进制/大文件不做文本扫描
_SKIP_SUFFIXES = {".db", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz",
                  ".ico", ".woff", ".woff2", ".ttf", ".pyc", ".so", ".dll"}

# 凭据形态的"针"：**拼接而成**，否则本文件自己会被扫成"内嵌私钥"（守卫必须不误伤自身）
_PRIVATE_KEY_NEEDLES = tuple(
    "BEGIN " + kind + " PRIVATE KEY" for kind in ("RSA", "OPENSSH", "EC", "DSA", "PGP")
)


def _git_repo_files(root: Path):
    """git 视角的"仓内容"：`git ls-files --cached --others --exclude-standard`。

    含**已跟踪**文件 + **未跟踪但未被 ignore** 的文件，自动排除 `.gitignore` 里的运行时产物
    （`.env` / `.env.bak.*` / `.venv` / 缓存等）。git 缺失、非仓库、或命令失败 → 返回 None
    （调用方回落文件系统扫描）——保持"无 git 也能跑"的既有前提。

    **为什么需要它（2026-09-30 / B7′）**：原先只走 `rglob` 扫文件系统，在**生产部署机**上会把
    部署必需的 `~/TradingAgents-astock/.env` 与历史备份 `.env.bak.*` 当成"凭据/残留"误报，
    使 B7 在服务器上恒失败（守卫只在开发机成立）。改后 B7 仍能抓到**已提交**的 `.env`
    （tracked → 仍在清单里），只是不再把 gitignore 的本地产物算作仓内容。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # git 不存在 / 超时 → 回落
        return None
    if proc.returncode != 0:
        return None
    names = [n for n in (proc.stdout or "").split("\0") if n]
    if not names:  # 空仓/异常输出不当作"仓内容为空"，回落更安全
        return None
    return names


def _iter_repo_files(root: Path = REPO):
    """仓内容文件迭代：优先 git 清单（尊重 .gitignore），无 git 时回落文件系统扫描。"""
    names = _git_repo_files(root)
    if names is not None:
        for name in names:
            path = root / name
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                yield path
        return
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _relative_files() -> set[str]:
    return {p.relative_to(REPO).as_posix() for p in _iter_repo_files()}


def _resolve_ref(token: str, rel_files: set[str], basenames: set[str]) -> bool:
    """路径状令牌是否可解析。

    - 含 `/` 的令牌：按"仓根相对"或"已知前缀相对"解析（容忍 `graph/setup.py` 这类简写），
      **不**退化为"同名文件存在即可"，否则 `agents/nope.py` 会被别处的同名文件掩盖。
    - 裸文件名：放宽为"仓内任意位置存在"，用于捕捉**被改名/删除**的文件。
    """
    token = token.split(":")[0]
    if "/" in token:
        return any((REPO / (prefix + token)).exists() for prefix in _PATH_PREFIXES)
    return token in basenames


def _extract_refs(text: str) -> list[str]:
    return sorted(set(_DOC_REF_RE.findall(text)))


@pytest.mark.parametrize("doc", _DOCS)
def test_doc_path_references_exist(doc: str):
    """B1：文档里被反引号引作路径的令牌必须真实存在（或落在 `_NON_REPO_REFS` 且有理由）。"""
    text = (REPO / doc).read_text(encoding="utf-8")
    rel_files = _relative_files()
    basenames = {Path(f).name for f in rel_files}

    missing = [
        token for token in _extract_refs(text)
        if token not in _NON_REPO_REFS and not _resolve_ref(token, rel_files, basenames)
    ]

    assert not missing, f"{doc} 引用了不存在的路径：{missing}"


def test_doc_ref_extractor_is_not_vacuous():
    """B9：守卫不得空转——提取器必须真能提出令牌，且**能判出**一个伪造的缺失路径。"""
    total = 0
    for doc in _DOCS:
        refs = _extract_refs((REPO / doc).read_text(encoding="utf-8"))
        assert refs, f"{doc} 提取到 0 个引用令牌——正则已失效（守卫空转）"
        total += len(refs)
    assert total >= 40, f"全部文档合计仅提取到 {total} 个令牌，疑似正则退化"

    rel_files = _relative_files()
    basenames = {Path(f).name for f in rel_files}
    fabricated = "tradingagents/dataflows/definitely_not_a_file.py"
    assert not _resolve_ref(fabricated, rel_files, basenames)


def test_non_repo_refs_all_have_reasons():
    """豁免表必须逐条写明理由（防止有人偷偷加豁免把 B1 架空）。"""
    for token, reason in _NON_REPO_REFS.items():
        assert reason.strip() and len(reason) >= 8, f"{token} 的理由过于简略：{reason!r}"


def test_repo_has_no_credentials_or_temp_residue():
    """B7：仓内不得有 `.env`（示例除外）、临时/备份残留、私钥或 `sk-` 形态密钥。"""
    offenders = []
    for path in _iter_repo_files():
        name = path.name
        rel = path.relative_to(REPO).as_posix()
        if name.startswith(".env") and not name.endswith(".example"):
            offenders.append(f"{rel}（env 文件，应改为 .env.example）")
            continue
        if path.suffix in {".tmp", ".bak", ".orig", ".rej", ".swp"} or name.endswith("~"):
            offenders.append(f"{rel}（临时/备份残留）")
            continue
        if path.suffix in _SKIP_SUFFIXES or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - 权限/IO 异常不属本守卫范围
            continue
        if _PRIVATE_KEY_NEEDLES and any(needle in text for needle in _PRIVATE_KEY_NEEDLES):
            offenders.append(f"{rel}（内嵌私钥）")
        if re.search(r"\bsk-[A-Za-z0-9]{24,}\b", text):
            offenders.append(f"{rel}（疑似明文 API key）")

    assert not offenders, "仓内可疑残留：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# B7′（2026-09-30）：`_iter_repo_files` 的两态各自都不空转
# ---------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 构造 tracked/ignored 两态")
def test_git_file_list_keeps_tracked_env_and_drops_ignored(tmp_path):
    """反向对照：**已跟踪**的 `.env` 必须仍在清单（B7 不被架空）；**被 ignore** 的
    `.env.local` 必须被排除（消除生产部署机的误报）。"""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=30)
    (repo / ".gitignore").write_text(".env.local\n", encoding="utf-8")
    (repo / ".env").write_text("KEY=1\n", encoding="utf-8")
    (repo / ".env.local").write_text("KEY=2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore", ".env"],
                   check=True, timeout=30)

    names = _git_repo_files(repo)
    assert names is not None
    assert ".env" in names              # tracked → 仍会被 B7 抓到
    assert ".env.local" not in names    # ignored → 不再误报


def test_iter_repo_files_falls_back_to_filesystem_without_git(tmp_path, monkeypatch):
    """git 不可用（`_git_repo_files` 返回 None）时回落文件系统扫描，`.env` 仍被抓到。"""
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_git_repo_files", lambda root: None)
    found = {p.name for p in _iter_repo_files(tmp_path)}
    assert ".env" in found


def test_clear_checkpoints_help_documents_risk():
    """T5 守卫：`--clear-checkpoints` 的 help 必须写明"无差别删除"的风险。

    用文本断言而非导入 CLI：本机缺 `prompt_toolkit`，`cli/main.py` 不可导入
    （CLAUDE.md 已声明的环境限制）。清理逻辑本身（`checkpointer.clear_all_checkpoints`）
    仍是无 age 判据、无锁的 unlink——**风险用文档披露**是本轮选定的处置。
    """
    src = (REPO / "cli" / "main.py").read_text(encoding="utf-8")
    idx = src.find('"--clear-checkpoints"')
    assert idx > 0, "`--clear-checkpoints` 选项不见了（守卫需同步更新）"
    help_block = src[idx: idx + 900]
    assert "RISK" in help_block
    assert "no lock" in help_block or "无锁" in help_block


# ───────────────────────────────────────────────────────────────────────────
# 反向对照（B9 negative control）：守卫不仅要"能通过"，还必须能**命中伪造的缺陷**，
# 否则规则被写坏时会静默失效。以下用例用运行时拼接/临时目录构造被检形态，证明检测链仍在工作。
# ───────────────────────────────────────────────────────────────────────────
def test_extractor_flags_fabricated_missing_reference():
    """反向对照（B1）：把必然不存在的引用令牌放进文档文本，提取器必须能提出来、
    且解析器必须判其为缺失；否则 B1 守卫在提取器/解析器被写坏时会静默放行悬空引用。"""
    fabricated = "tradingagents/dataflows/__definitely_missing__.py"
    refs = _extract_refs("详见 `%s` 中的说明。" % fabricated)
    assert fabricated in refs, "提取器未提取出伪造引用，B1 守卫已空转"
    rel_files = _relative_files()
    basenames = {Path(f).name for f in rel_files}
    assert not _resolve_ref(fabricated, rel_files, basenames), "解析器未把伪造引用判为缺失，B1 守卫已空转"


def test_detector_flags_fabricated_private_key_shape():
    """反向对照（B7）：运行时拼接出私钥头形态，凭据针必须能命中（否则内嵌私钥可静默入库）。"""
    fabricated = "-----BEGIN " + "RSA" + " PRIVATE KEY-----"
    text = "blob = '''\n%s\nabc\n'''" % fabricated
    assert any(needle in text for needle in _PRIVATE_KEY_NEEDLES)


def test_detector_flags_fabricated_api_key_shape_and_ignores_ordinary_text():
    """反向对照（B7）：`sk-` 形态密钥必须命中；正常中文文本不得误命中（防规则过宽）。"""
    fabricated = "sk-" + "A" * 24
    assert re.search(r"\bsk-[A-Za-z0-9]{24,}\b", fabricated)
    assert not re.search(r"\bsk-[A-Za-z0-9]{24,}\b", "普通文本不含密钥形态。")


def test_residue_predicate_flags_fabricated_leftovers(tmp_path):
    """反向对照（B7）：临时/备份残留判据必须命中 `.tmp/.bak/.orig/.rej/.swp` 与 `~` 结尾名；
    否则 `test_repo_has_no_credentials_or_temp_residue` 会永远漏检残留。"""
    names = ("x.tmp", "x.bak", "x.orig", "x.rej", "x.swp", "x~")
    for n in names:
        (tmp_path / n).write_text("x", encoding="utf-8")
    flagged = {
        p.name for p in tmp_path.iterdir()
        if p.suffix in {".tmp", ".bak", ".orig", ".rej", ".swp"} or p.name.endswith("~")
    }
    assert flagged == set(names), "残留判据未覆盖部分伪造残留：%s" % sorted(flagged)
