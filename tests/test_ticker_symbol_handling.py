import unittest

import pytest

# B1（2026-09-30）：`cli.*` 依赖 questionary/prompt_toolkit；缺该可选依赖时**跳过本文件**
# 而不是在收集期报 ModuleNotFoundError（后者会让裸跑 `pytest` 整体中断）。
pytest.importorskip("questionary", reason="CLI 可选依赖 questionary 未安装")

from cli.utils import normalize_ticker_symbol
from tradingagents.agents.utils.agent_utils import build_instrument_context


@pytest.mark.unit
class TickerSymbolHandlingTests(unittest.TestCase):
    def test_normalize_ticker_symbol_preserves_exchange_suffix(self):
        self.assertEqual(normalize_ticker_symbol(" cnc.to "), "CNC.TO")

    def test_build_instrument_context_mentions_exact_symbol(self):
        context = build_instrument_context("7203.T")
        self.assertIn("7203.T", context)
        self.assertIn("exchange suffix", context)


if __name__ == "__main__":
    unittest.main()
