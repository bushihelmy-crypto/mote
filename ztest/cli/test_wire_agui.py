#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the pure ``ViewEvent → AG-UI`` mapper (no transport)."""

from __future__ import annotations

import json

import pytest

from mote.product.interfaces.agui import wire as agui
from mote.product.presentation.events import events as ev


@pytest.fixture
def state() -> agui.AguiWireState:
    return agui.AguiWireState(thread_id="s1", run_id="r1")


# ── run lifecycle ───────────────────────────────────────────────────────────
def test_run_started_finished(state):
    assert agui.run_started(state) == {"type": "RUN_STARTED", "threadId": "s1", "runId": "r1"}
    assert agui.run_finished(state) == {"type": "RUN_FINISHED", "threadId": "s1", "runId": "r1"}


# ── streaming text message: start → content → end share one messageId ───────
def test_streaming_message_triple_shares_message_id(state):
    started = agui.to_agui_events(ev.MessageBlockStarted(role="assistant"), state)
    delta = agui.to_agui_events(ev.MessageBlockDelta(text="hel"), state)
    delta2 = agui.to_agui_events(ev.MessageBlockDelta(text="lo"), state)
    end = agui.to_agui_events(ev.MessageBlockCompleted(streamed=True), state)

    mid = started[0]["messageId"]
    assert started == [{"type": "TEXT_MESSAGE_START", "messageId": mid, "role": "assistant"}]
    assert delta == [{"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": "hel"}]
    assert delta2 == [{"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": "lo"}]
    assert end == [{"type": "TEXT_MESSAGE_END", "messageId": mid}]


def test_second_block_gets_new_message_id(state):
    first = agui.to_agui_events(ev.MessageBlockStarted(), state)[0]["messageId"]
    agui.to_agui_events(ev.MessageBlockCompleted(streamed=True), state)
    second = agui.to_agui_events(ev.MessageBlockStarted(), state)[0]["messageId"]
    assert first != second


def test_non_streamed_completed_synthesizes_full_triple(state):
    out = agui.to_agui_events(ev.MessageBlockCompleted(markdown="whole thing", streamed=False), state)
    types = [e["type"] for e in out]
    assert types == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    mid = out[0]["messageId"]
    assert all(e["messageId"] == mid for e in out)
    assert out[1]["delta"] == "whole thing"


# ── tool calls ──────────────────────────────────────────────────────────────
def test_tool_started_emits_start_and_args(state):
    out = agui.to_agui_events(ev.ToolCallStarted(tool_name="Bash", headline="ls -la", tool_use_id="tc1"), state)
    assert out == [
        {"type": "TOOL_CALL_START", "toolCallId": "tc1", "toolCallName": "Bash"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "tc1", "delta": "ls -la"},
    ]


def test_tool_started_without_preview_omits_args(state):
    out = agui.to_agui_events(ev.ToolCallStarted(tool_name="Noop", tool_use_id="tc9"), state)
    assert out == [{"type": "TOOL_CALL_START", "toolCallId": "tc9", "toolCallName": "Noop"}]


def test_tool_completed_emits_end_and_result(state):
    out = agui.to_agui_events(
        ev.ToolCallCompleted(tool_name="Bash", ok=True, summary="done", tool_use_id="tc1"),
        state,
    )
    assert out[0] == {"type": "TOOL_CALL_END", "toolCallId": "tc1"}
    assert out[1]["type"] == "TOOL_CALL_RESULT"
    assert out[1]["toolCallId"] == "tc1"
    assert out[1]["content"] == "done"


def test_tool_completed_failure_uses_recovery_as_content(state):
    out = agui.to_agui_events(
        ev.ToolCallCompleted(tool_name="Bash", ok=False, summary="", recovery="check the path", tool_use_id="tc2"),
        state,
    )
    assert out[1]["content"] == "check the path"


# ── steps / activities ──────────────────────────────────────────────────────
def test_task_progress_maps_to_step_started(state):
    out = agui.to_agui_events(ev.TaskProgress(stage="build", status="running"), state)
    assert out == [{"type": "STEP_STARTED", "stepName": "build"}]


def test_activity_started_and_completed(state):
    started = agui.to_agui_events(ev.ActivityStarted(activity_kind="graph", label="pipeline"), state)
    completed = agui.to_agui_events(ev.ActivityCompleted(outcome="success", summary="all ok"), state)
    assert started == [{"type": "STEP_STARTED", "stepName": "pipeline"}]
    assert completed == [{"type": "STEP_FINISHED", "stepName": "all ok"}]


# ── usage → state snapshot ──────────────────────────────────────────────────
def test_usage_maps_to_state_snapshot(state):
    out = agui.to_agui_events(
        ev.UsageUpdated(input_tokens=10, output_tokens=5, total_tokens=15, model="opus"),
        state,
    )
    assert out[0]["type"] == "STATE_SNAPSHOT"
    usage = out[0]["snapshot"]["usage"]
    assert usage["inputTokens"] == 10
    assert usage["totalTokens"] == 15
    assert usage["model"] == "opus"


# ── HITL / custom events ────────────────────────────────────────────────────
def test_approval_rides_custom(state):
    out = agui.to_agui_events(
        ev.ApprovalRequested(tool_name="Bash", action="rm -rf", approval_id="a1", risk="high"),
        state,
    )
    assert out[0]["type"] == "CUSTOM"
    assert out[0]["name"] == "approval"
    assert out[0]["value"]["approvalId"] == "a1"
    assert out[0]["value"]["risk"] == "high"


def test_question_rides_custom(state):
    out = agui.to_agui_events(ev.QuestionAsked(question="pick", options=["a", "b"]), state)
    assert out[0]["name"] == "question"
    assert out[0]["value"]["options"] == ["a", "b"]


def test_reasoning_rides_custom(state):
    out = agui.to_agui_events(ev.ReasoningDelta(text="hmm"), state)
    assert out == [{"type": "CUSTOM", "name": "reasoning", "value": {"delta": "hmm"}}]


def test_error_maps_to_run_error(state):
    out = agui.to_agui_events(ev.ErrorRaised(text="boom"), state)
    assert out == [{"type": "RUN_ERROR", "message": "boom"}]


def test_file_diff_rides_custom(state):
    out = agui.to_agui_events(ev.FileDiffBlock(path="/a.py", old="x", new="y"), state)
    assert out[0]["name"] == "fileDiff"
    assert out[0]["value"] == {"path": "/a.py", "old": "x", "new": "y"}


# ── unknown / display-only kinds → [] ───────────────────────────────────────
def test_transcript_cleared_maps_to_nothing(state):
    assert agui.to_agui_events(ev.TranscriptCleared(), state) == []


def test_system_reminder_maps_to_nothing(state):
    assert agui.to_agui_events(ev.SystemReminder(text="ctx"), state) == []


def test_unknown_event_maps_to_nothing(state):
    class Weird(ev.ViewEvent):
        kind = "weird_unknown_kind"

    assert agui.to_agui_events(Weird(), state) == []


def test_output_snapshot_and_invalidation_use_custom_events(state):
    snapshot = agui.to_agui_events(
        ev.OutputSnapshot(
            run_id="run-1",
            revision=1,
            schema_fingerprint="sha",
            value={"count": 7},
        ),
        state,
    )[0]
    invalidated = agui.to_agui_events(
        ev.OutputSnapshotInvalidated(run_id="run-1", revision=1, reason="stream_changed"),
        state,
    )[0]

    assert snapshot["type"] == "CUSTOM"
    assert snapshot["name"] == "outputSnapshot"
    assert snapshot["value"]["value"] == {"count": 7}
    assert invalidated["name"] == "outputSnapshotInvalidated"


def test_committed_output_is_distinct_from_snapshot(state):
    output = agui.to_agui_events(
        ev.OutputCommitted(
            run_id="run-1",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 7},
        ),
        state,
    )[0]

    assert output["name"] == "outputCommitted"
    assert output["value"]["runId"] == "run-1"


# ── SSE encoding: single line, valid JSON, trailing blank line ──────────────
def test_encode_sse_single_line_json():
    frame = agui.encode_sse({"type": "RUN_STARTED", "threadId": "s1", "runId": "r1"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    body = frame[len("data: ") : -2]
    assert "\n" not in body
    assert json.loads(body) == {"type": "RUN_STARTED", "threadId": "s1", "runId": "r1"}


def test_encode_sse_preserves_unicode():
    frame = agui.encode_sse({"type": "CUSTOM", "name": "notice", "value": {"text": "你好"}})
    assert "你好" in frame
