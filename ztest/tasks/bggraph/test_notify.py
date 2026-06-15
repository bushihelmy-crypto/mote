#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for bggraph notification rendering (:mod:`metagpt.tasks.bggraph.notify`).

Each ``push_*`` helper routes through ``report_progress``; the tests install a
collecting progress writer (via :func:`set_progress_writer`) and assert on the
captured text.  Also covers the ``report_progress`` no-op-outside-context
contract and the ``_render_*`` / ``_resolve_param_source`` helpers.
"""
from __future__ import annotations

import pytest

from metagpt.tasks.bggraph import END, START, BatchFailureError, BgGraph, NodeStatus
from metagpt.tasks.bggraph.notify import (
    _render_completed_nodes,
    _resolve_param_source,
    push_llm_route_notification,
    push_node_failure_notification,
    push_started_notification,
    push_terminal_notification,
)
from metagpt.tasks.bggraph.report import (
    report_progress,
    reset_progress_writer,
    set_progress_writer,
)

from .conftest import S, sync_node


class _Collector:
    """A progress writer that records (stage, status, detail) tuples."""

    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, stage, status, detail=None):
        self.events.append((stage, status, detail))

    @property
    def text(self) -> str:
        return "\n".join(str(d) for _, _, d in self.events)


@pytest.fixture
def collector():
    c = _Collector()
    token = set_progress_writer(c)
    try:
        yield c
    finally:
        reset_progress_writer(token)


def _build_graph() -> BgGraph:
    g = BgGraph("media", state_schema=S)
    g.add_node("split", sync_node(lambda s: {"parts": 3}), params={"src": {"from": "$input.x"}})
    g.add_node("tts", sync_node(lambda s: "audio"))
    g.add_node("render", sync_node(lambda s: "video"))
    g.add_node("merge", sync_node(lambda s: "final"))
    g.add_edge(START, "split")
    g.add_edge("split", "tts")
    g.add_edge("split", "render")
    g.add_edge(["tts", "render"], "merge")
    g.add_edge("merge", END)
    return g


class TestReportProgressContract:
    def test_noop_outside_context(self):
        # No writer installed → must not raise.
        report_progress("x", NodeStatus.RUNNING, "detail")

    def test_writer_receives_event(self, collector):
        report_progress("split", NodeStatus.COMPLETED, {"parts": 3})
        assert collector.events == [("split", NodeStatus.COMPLETED, {"parts": 3})]


class TestPushStarted:
    def test_started_includes_stage_summary(self, collector):
        g = _build_graph()
        push_started_notification(g, task_id="bg_1")
        assert len(collector.events) == 1
        stage, status, detail = collector.events[0]
        assert status == NodeStatus.RUNNING
        assert "media" in detail
        assert "stage-summary" in detail
        assert "split" in detail


class TestPushTerminal:
    def test_success(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        push_terminal_notification(
            g, state, NodeStatus.COMPLETED, result="final", initial_params={"x": 1}
        )
        detail = collector.events[-1][2]
        assert "completed" in detail
        assert "final" in detail

    def test_failure_lists_failed_node(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        err = BatchFailureError([("tts", ValueError("boom tts"))])
        push_terminal_notification(
            g, state, NodeStatus.FAILED, error=err, initial_params={"x": 1}
        )
        detail = collector.events[-1][2]
        assert "failed" in detail
        assert "tts" in detail
        assert "boom tts" in detail


class TestPushNodeFailure:
    def test_node_failure_block(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        exc = ValueError("render crashed")
        push_node_failure_notification(
            "render",
            exc,
            state,
            g,
            prior_errors=[("tts", RuntimeError("tts earlier"))],
            completed={"split"},
            running_names=["merge"],
        )
        detail = collector.events[-1][2]
        assert "node_failed" in detail
        assert "render crashed" in detail
        assert "tts earlier" in detail  # other failed nodes
        assert "merge" in detail  # running nodes


class TestPushLlmRoute:
    def test_llm_route_options(self, collector):
        g = BgGraph("llm", state_schema=S)
        g.add_node("a", sync_node(lambda s: "a-done"))
        g.add_node("go", sync_node(lambda s: "went"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick the next move", {"go": "go", "stop": END})
        g.add_edge("go", END)
        state = g.state_schema(x=1)
        setattr(state, "a", "a-done")
        edge = g._llm_edges[0]
        push_llm_route_notification(edge, state, g)
        detail = collector.events[-1][2]
        assert "waiting_for_route" in detail
        assert "Pick the next move" in detail
        assert "go" in detail
        # END is an option → the optional hint is used.
        assert "may also do nothing" in detail


class TestHelpers:
    def test_resolve_input_param_from_initial(self):
        state = S(x=5)
        assert _resolve_param_source("$input.x", state, {"x": 9}) == 9

    def test_resolve_input_param_from_state(self):
        state = S(x=5)
        assert _resolve_param_source("$input.x", state, {}) == 5

    def test_resolve_node_output_key(self):
        state = S(x=0)
        setattr(state, "split", {"parts": 3})
        assert _resolve_param_source("split.parts", state, {}) == 3

    def test_render_completed_nodes_filters_status(self):
        g = _build_graph()
        state = g.state_schema(x=0)
        # Only 'split' has completed and has a result.
        setattr(state, "split", {"parts": 3})
        g._nodes["split"].status = NodeStatus.COMPLETED
        text = _render_completed_nodes(g, state, {"split", "tts"})
        assert "split" in text
        assert "tts" not in text  # tts has no result / not completed
