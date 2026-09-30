"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


@pytest.fixture(autouse=True)
def _isolated_local_caches(monkeypatch, tmp_path):
    """把本地累积缓存重定向到临时目录（2026-09-11）。

    否则任何调用 ``get_fund_flow`` / ``get_northbound_flow`` 的测试都会写到开发者的
    真实缓存目录（``~/.tradingagents/cache``）；在原子替换被沙箱/权限拒绝时还会留下
    ``.fundflow_*.tmp`` 残留。本 fixture 只兜底，测试内显式 patch 仍然优先。
    """
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(
        a_stock, "_fund_flow_cache_path",
        lambda: str(tmp_path / "fund_flow_daily.csv"),
    )
    monkeypatch.setattr(
        a_stock, "_northbound_cache_path",
        lambda: str(tmp_path / "northbound_daily.csv"),
    )


@pytest.fixture(autouse=True)
def _isolated_missing_data_index(monkeypatch, tmp_path):
    """把「缺失数据任务」索引与缓存重定向到临时目录（T4 移植 missing_data）。

    与 `_isolated_local_caches` 同因：`missing_data` 的索引/缓存路径硬编码在
    `~/.tradingagents/` 下，任何跑**完整管线**的用例（如 `test_memory_log` /
    `test_checkpoint_resume`）在 `finalize_graph_run` 阶段都会去写开发者的真实 HOME；
    在原子替换被沙箱或权限拒绝时直接 `PermissionError`（本机实测 2 例），
    并在 HOME 留下残留文件。测试内显式 patch 仍然优先。
    """
    from tradingagents.dataflows import missing_data

    monkeypatch.setattr(
        missing_data, "_MISSING_DATA_TASKS_FILE", tmp_path / "missing_data_tasks.json"
    )
    monkeypatch.setattr(
        missing_data, "_MISSING_DATA_CACHE_DIR", tmp_path / "missing_data_cache"
    )
    missing_data._INDEX_CACHE.clear()
    yield
    missing_data._INDEX_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_em_breaker():
    """重置东财行情集群熔断状态（0.5.27）。

    `_em_fail_streak` / `_em_breaker_until` 是模块级状态：某个用例制造了 3 次连接失败后，
    熔断会**跨用例**生效，让后续用例的 `_em_get` 直接快速失败（断言 hosts_called 就会莫名其妙地空）。
    """
    from tradingagents.dataflows import a_stock

    a_stock._em_fail_streak = 0
    a_stock._em_breaker_until = 0.0
    yield
    a_stock._em_fail_streak = 0
    a_stock._em_breaker_until = 0.0


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
