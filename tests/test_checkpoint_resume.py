"""Test checkpoint resume: crash mid-analysis, re-run resumes from last node."""

import functools
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)

# Mutable flag to simulate crash on first run
_should_crash = False


class _SimpleState(TypedDict):
    count: int


def _node_a(state: _SimpleState) -> dict:
    return {"count": state["count"] + 1}


def _node_b(state: _SimpleState) -> dict:
    if _should_crash:
        raise RuntimeError("simulated mid-analysis crash")
    return {"count": state["count"] + 10}


def _build_graph() -> StateGraph:
    builder = StateGraph(_SimpleState)
    builder.add_node("analyst", _node_a)
    builder.add_node("trader", _node_b)
    builder.set_entry_point("analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


class TestCheckpointResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_crash_and_resume(self):
        """Crash at 'trader' node, then resume from checkpoint."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Run 1: crash at trader node
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        # Checkpoint should exist at step 1 (analyst completed)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))
        step = checkpoint_step(self.tmpdir, self.ticker, self.date)
        self.assertEqual(step, 1)

        # Run 2: resume — trader succeeds this time
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke(None, config=cfg)

        # analyst added 1, trader added 10 → 11
        self.assertEqual(result["count"], 11)

    def test_clear_checkpoint_allows_fresh_start(self):
        """After clearing, the graph starts from scratch."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Create a checkpoint by crashing
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Clear it
        clear_checkpoint(self.tmpdir, self.ticker, self.date)
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Fresh run succeeds from scratch
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config=cfg)

        self.assertEqual(result["count"], 11)


    def test_different_date_starts_fresh(self):
        """A different date must NOT resume from an existing checkpoint."""
        global _should_crash
        builder = _build_graph()
        date2 = "2026-04-21"

        # Run with date1 — crash to leave a checkpoint
        _should_crash = True
        tid1 = thread_id(self.ticker, self.date)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # date2 should have no checkpoint
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, date2))

        # Run with date2 — should start fresh and succeed
        _should_crash = False
        tid2 = thread_id(self.ticker, date2)
        self.assertNotEqual(tid1, tid2)

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})

        # Fresh run: analyst +1, trader +10 = 11
        self.assertEqual(result["count"], 11)

        # Original date checkpoint still exists (untouched)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

    def test_trading_graph_prepare_uses_none_input_when_resuming(self):
        """TradingAgentsGraph must resume with None input, not a fresh state.

        断点 key 现在含"配置指纹"（模型 / 分析师集合等），所以造断点时必须用同一
        指纹——否则配置一变就被当成新跑，正是要防的静默续跑。
        """
        global _should_crash
        builder = _build_graph()

        fake_graph = MagicMock()
        fake_graph.config = {
            "checkpoint_enabled": True,
            "data_cache_dir": self.tmpdir,
        }
        fake_graph.selected_analysts = ["market"]
        # MagicMock 会把 _run_fingerprint 变成子 mock（返回另一个 MagicMock），
        # 指纹里就会带上随机的 mock id。必须绑到真实实现上。
        fake_graph._run_fingerprint = functools.partial(
            TradingAgentsGraph._run_fingerprint, fake_graph
        )
        tid = thread_id(
            self.ticker, self.date, TradingAgentsGraph._run_fingerprint(fake_graph)
        )
        cfg = {"configurable": {"thread_id": tid}}

        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        fake_graph.workflow = builder
        fake_graph._checkpointer_ctx = None
        fake_graph.propagator.get_graph_args.return_value = {
            "stream_mode": "values",
            "config": {"recursion_limit": 100},
        }

        init_state, args, step = TradingAgentsGraph.prepare_graph_run(
            fake_graph,
            self.ticker,
            self.date,
        )

        self.assertIsNone(init_state)
        self.assertEqual(step, 1)
        self.assertEqual(args["config"]["configurable"]["thread_id"], tid)
        fake_graph.propagator.create_initial_state.assert_not_called()

        TradingAgentsGraph.close_graph_run(fake_graph)

    def test_config_change_does_not_resume_stale_checkpoint(self):
        """换了模型/分析师集合后重跑同一天 → 视为新跑，不得续用旧状态。

        旧实现只用 (ticker, date) 做 key：改配置后重跑会静默接着旧断点跑，
        已完成阶段用旧模型、剩余阶段用新模型，报告里完全看不出来。
        """
        global _should_crash
        builder = _build_graph()

        fake_graph = MagicMock()
        fake_graph.config = {
            "checkpoint_enabled": True,
            "data_cache_dir": self.tmpdir,
            "deep_think_llm": "model-a",
        }
        fake_graph.selected_analysts = ["market"]
        fake_graph._run_fingerprint = functools.partial(
            TradingAgentsGraph._run_fingerprint, fake_graph
        )

        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke(
                    {"count": 0},
                    config={
                        "configurable": {
                            "thread_id": thread_id(
                                self.ticker, self.date,
                                TradingAgentsGraph._run_fingerprint(fake_graph),
                            )
                        }
                    },
                )

        # 换模型后配置指纹改变
        fake_graph.config = dict(fake_graph.config, deep_think_llm="model-b")
        fake_graph.workflow = builder
        fake_graph._checkpointer_ctx = None
        fake_graph.propagator.get_graph_args.return_value = {"config": {}}
        fake_graph.propagator.create_initial_state.return_value = {"count": 0}

        init_state, args, step = TradingAgentsGraph.prepare_graph_run(
            fake_graph,
            self.ticker,
            self.date,
        )

        self.assertIsNone(step, "配置变了却仍然命中旧断点")
        self.assertIsNotNone(init_state, "配置变了必须重建初始状态（开新跑）")

        TradingAgentsGraph.close_graph_run(fake_graph)


if __name__ == "__main__":
    unittest.main()
