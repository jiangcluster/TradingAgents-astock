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


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
