#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for bggraph notification rendering (:mod:`mote.runtime.tools.bggraph.notify`).

Each ``push_*`` helper routes through ``report_progress``; the tests install a
collecting progress writer (via :func:`set_progress_writer`) and assert on the
captured text.  Also covers the ``report_progress`` no-op-outside-context
contract and the ``_render_*`` / ``_resolve_param_source`` helpers.
"""

from __future__ import annotations

from typing import Any

import pytest

from mote.contracts.workflow.identity import WorkflowDefinitionId
from mote.orchestration.workflows import END, START, GraphBatchFailureError, WorkflowBuilder, WorkflowNodeStatus
from mote.orchestration.workflows.events import emit_workflow_progress, reset_progress_sink, set_progress_sink
from mote.orchestration.workflows.notify import (
    _render_completed_nodes,
    _render_status_nodes,
    _resolve_param_source,
    push_llm_route_notification,
    push_node_notification,
    push_started_notification,
    push_terminal_notification,
)
from mote.orchestration.workflows.types import GraphRunState

from .conftest import S, sync_node


class NotifyState(S):
    split: Any = None
    tts: Any = None
    render: Any = None
    merge: Any = None
    a: Any = None
    go: Any = None


class _Collector:
    """A progress writer that records (stage, status, detail) tuples."""

    def __init__(self):
        self.events: list[tuple] = []

    def emit(self, event):
        self.events.append((event.stage, WorkflowNodeStatus(event.phase.value), event.detail))

    @property
    def text(self) -> str:
        return "\n".join(str(d) for _, _, d in self.events)


@pytest.fixture
def collector():
    c = _Collector()
    token = set_progress_sink(c)
    try:
        yield c
    finally:
        reset_progress_sink(token)


def _build_graph() -> WorkflowBuilder:
    g = WorkflowBuilder("media", state_schema=NotifyState)
    g._definition_id = WorkflowDefinitionId("definition-media")
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


def _ring_graph() -> WorkflowBuilder:
    """A ``work`` node that self-loops (mirrors code_review's review_batch)."""
    g = WorkflowBuilder("ring", state_schema=NotifyState)
    g._definition_id = WorkflowDefinitionId("definition-ring")
    g.add_node("work", sync_node(lambda s: {"x": s.x}))
    g.add_node("done", sync_node(lambda s: "ok"))
    g.add_edge(START, "work")
    g.add_conditional_edges(
        "work",
        lambda s: "loop" if s.x else "done",
        {"loop": "work", "done": "done"},
        projector=lambda state: state,
    )
    g.add_edge("done", END)
    return g


def _run_state(g: WorkflowBuilder, **statuses: WorkflowNodeStatus) -> GraphRunState:
    """Build a run state for *g* with the given per-node statuses applied.

    Node execution status now lives on the per-run :class:`GraphRunState` (not
    the shared graph definition), so notification tests set status here.
    """
    rs = GraphRunState.for_graph(g)
    for name, status in statuses.items():
        rs.get(name).status = status
    return rs


class TestReportProgressContract:
    def test_noop_outside_context(self):
        graph = _build_graph()
        emit_workflow_progress(
            graph,
            GraphRunState.for_graph(graph),
            "x",
            WorkflowNodeStatus.RUNNING,
            "detail",
        )

    def test_writer_receives_event(self, collector):
        graph = _build_graph()
        emit_workflow_progress(
            graph,
            GraphRunState.for_graph(graph),
            "split",
            WorkflowNodeStatus.SUCCESS,
            "parts: 3",
        )
        assert collector.events == [("split", WorkflowNodeStatus.SUCCESS, "parts: 3")]


class TestPushStarted:
    def test_started_includes_stage_summary(self, collector):
        g = _build_graph()
        push_started_notification(g, GraphRunState.for_graph(g))
        assert len(collector.events) == 1
        stage, status, detail = collector.events[0]
        assert status == WorkflowNodeStatus.RUNNING
        assert "media" in detail
        assert "stage-summary" in detail
        assert "split" in detail


class TestPushTerminal:
    def test_success(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        push_terminal_notification(g, state, WorkflowNodeStatus.SUCCESS, result="final", initial_params={"x": 1})
        detail = collector.events[-1][2]
        assert "succeeded" in detail
        assert "final" in detail

    def test_failure_lists_failed_node(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        err = GraphBatchFailureError([("tts", ValueError("boom tts"))])
        push_terminal_notification(g, state, WorkflowNodeStatus.FAILED, error=err, initial_params={"x": 1})
        detail = collector.events[-1][2]
        assert "failed" in detail
        assert "tts" in detail
        assert "boom tts" in detail

    def test_failure_dedups_repeated_node(self, collector):
        # Cyclic re-run: the same node fails twice → listed once.
        g = _build_graph()
        state = g.state_schema(x=1)
        err = GraphBatchFailureError([("tts", ValueError("boom1")), ("tts", ValueError("boom2"))])
        push_terminal_notification(g, state, WorkflowNodeStatus.FAILED, error=err)
        detail = collector.events[-1][2]
        failed_seg = detail.split("failed nodes:")[1].split("waiting_for_route")[0]
        # Last-wins → only the second error text; node listed once in failed.
        assert failed_seg.count("- tts\n") == 1
        assert "boom2" in detail and "boom1" not in detail

    def test_failed_node_not_in_completed(self, collector):
        # Cyclic fail→succeed: node is in the failure list AND status SUCCESS.
        g = _build_graph()
        state = g.state_schema(x=1)
        setattr(state, "tts", "audio")
        g._nodes["tts"].status = WorkflowNodeStatus.SUCCESS
        err = GraphBatchFailureError([("tts", ValueError("boom"))])
        push_terminal_notification(g, state, WorkflowNodeStatus.FAILED, error=err)
        detail = collector.events[-1][2]
        failed_seg = detail.split("failed nodes:")[1].split("waiting_for_route")[0]
        completed_seg = detail.split("completed nodes:")[1].split("skipped nodes:")[0]
        assert "tts" in failed_seg
        assert "tts" not in completed_seg  # failed takes precedence

    def test_terminal_waiting_always_none(self, collector):
        # Graph is never paused on an LLM route at terminal.
        g = WorkflowBuilder("llm", state_schema=NotifyState)
        g._definition_id = WorkflowDefinitionId("definition-llm-terminal")
        g.add_node("a", sync_node(lambda s: "a-done"))
        g.add_node("go", sync_node(lambda s: "went"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick", {"go": "go", "stop": END})
        g.add_edge("go", END)
        state = g.state_schema(x=1)
        setattr(state, "a", "a-done")
        g._nodes["a"].status = WorkflowNodeStatus.SUCCESS
        push_terminal_notification(g, state, WorkflowNodeStatus.SUCCESS, result="went")
        # Success path uses a different template, so force a FAILED terminal to
        # exercise the waiting section explicitly.
        push_terminal_notification(
            g, state, WorkflowNodeStatus.FAILED, error=GraphBatchFailureError([("go", ValueError("x"))])
        )
        detail = collector.events[-1][2]
        waiting_seg = detail.split("waiting_for_route nodes:")[1].split("completed nodes:")[0]
        assert "(none)" in waiting_seg
        assert "- a\n" not in waiting_seg


class TestPushNodeFailure:
    def test_node_failure_block(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        exc = ValueError("render crashed")
        push_node_notification(
            "render",
            WorkflowNodeStatus.FAILED,
            state,
            g,
            completed={"split"},
            running_names=["merge"],
            exc=exc,
        )
        detail = collector.events[-1][2]
        assert "node_failed" in detail
        assert "render crashed" in detail
        assert "merge" in detail  # running nodes
        # Intermediate note carries no historical failure state.
        assert "other failed nodes" not in detail

    def test_node_failure_buckets_nodes_by_status(self, collector):
        # split=SUCCESS, tts=SKIPPED, merge=PENDING; render just failed.
        g = _build_graph()
        state = g.state_schema(x=1)
        setattr(state, "split", {"parts": 3})
        rs = _run_state(
            g,
            split=WorkflowNodeStatus.SUCCESS,
            tts=WorkflowNodeStatus.SKIPPED,
            merge=WorkflowNodeStatus.PENDING,
            render=WorkflowNodeStatus.FAILED,
        )
        push_node_notification(
            "render",
            WorkflowNodeStatus.FAILED,
            state,
            g,
            completed={"split"},
            running_names=[],
            run_state=rs,
            exc=ValueError("render crashed"),
        )
        detail = collector.events[-1][2]
        # Each section header is present, and nodes land in the right bucket.
        for header in (
            "node fail:",
            "waiting_for_route nodes:",
            "running nodes:",
            "completed nodes:",
            "skipped nodes:",
            "pending nodes:",
        ):
            assert header in detail
        completed_seg = detail.split("completed nodes:")[1]
        skipped_seg = completed_seg.split("skipped nodes:")[1]
        pending_seg = skipped_seg.split("pending nodes:")[1]
        completed_seg = completed_seg.split("skipped nodes:")[0]
        skipped_seg = skipped_seg.split("pending nodes:")[0]
        assert "split" in completed_seg
        assert "tts" in skipped_seg and "tts" not in completed_seg
        assert "merge" in pending_seg

    def test_node_status_blocks_drop_description(self, collector):
        # Identity-only sections (success subject, completed/running/skipped/
        # pending) and the failed block are now bare name (+ error for failed):
        # no ``description:`` line anywhere in a node notification.
        g = _build_graph()
        state = g.state_schema(x=1)
        setattr(state, "split", {"parts": 3})
        g._nodes["split"].status = WorkflowNodeStatus.SUCCESS
        g._nodes["tts"].status = WorkflowNodeStatus.SKIPPED
        g._nodes["merge"].status = WorkflowNodeStatus.PENDING
        push_node_notification(
            "render",
            WorkflowNodeStatus.FAILED,
            state,
            g,
            completed={"split"},
            running_names=[],
            exc=ValueError("render crashed"),
        )
        detail = collector.events[-1][2]
        assert "description:" not in detail
        assert "render crashed" in detail  # failed block still carries the error

    def test_node_success_subject_is_bare_name(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        push_node_notification(
            "tts",
            WorkflowNodeStatus.SUCCESS,
            state,
            g,
            completed={"tts"},
            running_names=[],
        )
        detail = collector.events[-1][2]
        assert "- tts" in detail
        assert "description:" not in detail

    def test_ring_node_success_annotates_lap_not_retry(self, collector):
        # A self-loop node emits one success notification per lap; the subject
        # must say it is a ring lap (not a stall/retry) so repeated identical
        # completions are not misread as the graph being stuck / restarting.
        g = _ring_graph()
        state = g.state_schema(x=1)
        rs = GraphRunState.for_graph(g)
        rs.get("work").attempts = 3  # third lap around the ring
        push_node_notification(
            "work",
            WorkflowNodeStatus.SUCCESS,
            state,
            g,
            completed={"work"},
            running_names=[],
            run_state=rs,
        )
        detail = collector.events[-1][2]
        assert "- work" in detail
        assert "ring" in detail
        assert "lap 3" in detail
        # A non-ring node in the same graph must NOT get the lap annotation.
        collector.events.clear()
        push_node_notification(
            "done",
            WorkflowNodeStatus.SUCCESS,
            state,
            g,
            completed={"done"},
            running_names=[],
        )
        assert "lap" not in collector.events[-1][2]

    def test_node_failure_action_hint_running(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        push_node_notification(
            "render",
            WorkflowNodeStatus.FAILED,
            state,
            g,
            completed=set(),
            running_names=["merge"],
            exc=ValueError("x"),
        )
        assert "Other nodes are still running" in collector.events[-1][2]

    def test_node_failure_action_hint_stalled(self, collector):
        g = _build_graph()
        state = g.state_schema(x=1)
        push_node_notification(
            "render",
            WorkflowNodeStatus.FAILED,
            state,
            g,
            completed=set(),
            running_names=[],
            exc=ValueError("x"),
        )
        assert "No runnable nodes remain" in collector.events[-1][2]


class TestPushLlmRoute:
    def test_llm_route_options(self, collector):
        g = WorkflowBuilder("llm", state_schema=NotifyState)
        g._definition_id = WorkflowDefinitionId("definition-llm-route")
        g.add_node("a", sync_node(lambda s: "a-done"))
        g.add_node("go", sync_node(lambda s: "went"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick the next move", {"go": "go", "stop": END})
        g.add_edge("go", END)
        state = g.state_schema(x=1)
        setattr(state, "a", "a-done")
        edge = g._llm_edges[0]
        push_llm_route_notification(edge, state, g, GraphRunState.for_graph(g))
        detail = collector.events[-1][2]
        assert "waiting_for_route" in detail
        assert "Pick the next move" in detail
        assert "go" in detail
        # END is an option → the optional hint is used.
        assert "may also do nothing" in detail


class TestHelpers:
    def test_resolve_input_param_from_initial(self):
        state = NotifyState(x=5)
        assert _resolve_param_source("$input.x", state, {"x": 9}) == 9

    def test_resolve_input_param_from_state(self):
        state = NotifyState(x=5)
        assert _resolve_param_source("$input.x", state, {}) == 5

    def test_resolve_node_output_key(self):
        state = NotifyState(x=0)
        setattr(state, "split", {"parts": 3})
        assert _resolve_param_source("split.parts", state, {}) == 3

    def test_render_completed_nodes_filters_status(self):
        g = _build_graph()
        state = g.state_schema(x=0)
        # Only 'split' has completed and has a result.
        setattr(state, "split", {"parts": 3})
        rs = _run_state(g, split=WorkflowNodeStatus.SUCCESS)
        text = _render_completed_nodes(g, state, rs, {"split", "tts"})
        assert "split" in text
        assert "tts" not in text  # tts has no result / not completed

    def test_render_completed_excludes_skipped(self):
        g = _build_graph()
        state = g.state_schema(x=0)
        setattr(state, "split", {"parts": 3})
        rs = _run_state(g, split=WorkflowNodeStatus.SKIPPED)
        # SKIPPED no longer surfaces in the completed section.
        assert "split" not in _render_completed_nodes(g, state, rs, {"split"})

    def test_render_completed_excludes_waiting_for_route(self):
        g = WorkflowBuilder("llm", state_schema=NotifyState)
        g.add_node("a", sync_node(lambda s: "a-done"))
        g.add_node("go", sync_node(lambda s: "went"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick", {"go": "go", "stop": END})
        g.add_edge("go", END)
        state = g.state_schema(x=1)
        setattr(state, "a", "a-done")
        rs = _run_state(g, a=WorkflowNodeStatus.SUCCESS)
        # 'a' is parked on an LLM route → reported as waiting, not completed.
        assert "a" not in _render_completed_nodes(g, state, rs, {"a"})

    def test_render_status_nodes(self):
        g = _build_graph()
        rs = _run_state(g, tts=WorkflowNodeStatus.SKIPPED, merge=WorkflowNodeStatus.PENDING)
        assert "tts" in _render_status_nodes(g, rs, WorkflowNodeStatus.SKIPPED)
        assert "merge" in _render_status_nodes(g, rs, WorkflowNodeStatus.PENDING)
        assert _render_status_nodes(g, rs, WorkflowNodeStatus.TIMEOUT) == "  (none)"
