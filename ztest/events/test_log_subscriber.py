#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for LogSubscriber: it logs semantic events, never folds, never raises."""
from __future__ import annotations

import pytest

from mote.runtime.events import (
    AgentLifecycleEvent,
    LLMStreamDeltaEvent,
    LogSubscriber,
    RecoveryEvent,
    ResourceReportEvent,
    SessionStartEvent,
    TaskProgressEvent,
    ToolInvocationStartedEvent,
    bind_telemetry,
    observe_event,
    observe_event_sync,
)
from mote.runtime.events.log_subscriber import _clip
from mote.ztest.telemetry import InlineTelemetry


def _capture(monkeypatch):
    """Patch the subscriber's logger; return (info_lines, debug_lines, warns)."""
    info, debug, warns = [], [], []
    import mote.runtime.events.log_subscriber as mod

    monkeypatch.setattr(mod.logger, "info", lambda m: info.append(m))
    monkeypatch.setattr(mod.logger, "debug", lambda m: debug.append(m))
    monkeypatch.setattr(mod.logger, "warning", lambda m: warns.append(m))
    return info, debug, warns


@pytest.mark.asyncio
async def test_session_start_logged_at_info(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    sub = LogSubscriber()
    out = await sub.handle(SessionStartEvent(session_id="abcd1234ef", source="startup", model="gpt"))
    assert out is None  # never folds an outcome
    assert len(info) == 1 and "session_start" in info[0] and "abcd1234" in info[0]
    assert debug == []


@pytest.mark.asyncio
async def test_tool_event_logged_at_debug(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    await LogSubscriber().handle(ToolInvocationStartedEvent(tool_name="Read", tool_input={"x": 1}))
    assert info == []
    assert len(debug) == 1 and "tool_invocation_started" in debug[0] and "Read" in debug[0]


@pytest.mark.asyncio
async def test_handle_swallows_errors(monkeypatch):
    info, debug, warns = _capture(monkeypatch)

    class _Boom:
        name = "boom"

        def __getattr__(self, _):  # any field access explodes
            raise RuntimeError("nope")

    # An unknown event type hits the else-branch which only reads .name — make
    # even that raise to exercise the guard.
    bad = _Boom()
    out = await LogSubscriber().handle(bad)
    assert out is None  # best-effort: never raises into the bus


@pytest.mark.asyncio
async def test_stream_deltas_are_not_logged(monkeypatch):
    # Deltas are delivered via emit_sync -> handle_sync, which LogSubscriber does
    # not implement, so per-token chunks never reach the logger.
    info, debug, _ = _capture(monkeypatch)
    telemetry = InlineTelemetry(LogSubscriber())
    with bind_telemetry(telemetry):
        observe_event_sync(LLMStreamDeltaEvent(token="tok"))
    assert info == [] and debug == []


@pytest.mark.asyncio
async def test_registered_handler_logs_via_emit(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    telemetry = InlineTelemetry(LogSubscriber())
    with bind_telemetry(telemetry):
        await observe_event(SessionStartEvent(session_id="zzzz", source="resume"))
    assert any("session_start" in line for line in info)


def test_clip_collapses_whitespace():
    assert _clip("a  b\n c") == "a b c"
    # No truncation: long text is returned intact (only whitespace collapsed).
    long = "x" * 200
    assert _clip(long) == long


# ---------------------------------------------------------------------------
# Unified-path events: recovery / resource_report / task_progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["recovered", "give_up"])
async def test_recovery_event_logged_at_info(monkeypatch, phase):
    info, debug, _ = _capture(monkeypatch)
    await LogSubscriber().handle(
        RecoveryEvent(
            phase=phase,
            action="rotate_credential",
            attempt=1,
            error_type="LLMError",
            error="429",
        )
    )
    assert debug == []
    assert len(info) == 1 and "recovery" in info[0] and phase in info[0] and "rotate_credential" in info[0]


@pytest.mark.asyncio
async def test_resource_report_event_logged_at_debug(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    await LogSubscriber().handle(ResourceReportEvent(block="Terminal", name_="content", role="dev"))
    assert info == []
    assert len(debug) == 1 and "resource_report" in debug[0] and "Terminal" in debug[0]


def test_task_progress_logged_via_handle_sync(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    LogSubscriber().handle_sync(TaskProgressEvent(task_id="bg_1", stage="split", status="running", detail="d"))
    assert info == []
    assert len(debug) == 1 and "task_progress" in debug[0] and "bg_1" in debug[0]


def test_handle_sync_ignores_non_task_progress(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    LogSubscriber().handle_sync(LLMStreamDeltaEvent(token="tok"))
    LogSubscriber().handle_sync(ResourceReportEvent(block="Terminal", name_="content"))
    assert info == [] and debug == []


@pytest.mark.asyncio
async def test_async_handle_ignores_task_progress(monkeypatch):
    # TaskProgress rides the sync fan-out; the async path must not double-log it.
    info, debug, _ = _capture(monkeypatch)
    out = await LogSubscriber().handle(TaskProgressEvent(task_id="bg_1", stage="s", status="running"))
    assert out is None
    assert info == [] and debug == []


# ---------------------------------------------------------------------------
# Agent lifecycle: emitted by control / residency telemetry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["added", "rehydrated", "evicted", "interrupted"])
def test_agent_lifecycle_logged_via_handle_sync(monkeypatch, phase):
    info, debug, _ = _capture(monkeypatch)
    LogSubscriber().handle_sync(AgentLifecycleEvent(session_id="abcd1234ef", phase=phase, detail="Role"))
    assert debug == []
    assert len(info) == 1 and "agent_lifecycle" in info[0] and phase in info[0] and "abcd1234" in info[0]


def test_agent_lifecycle_detail_optional(monkeypatch):
    # No detail -> no trailing segment, but still logged at info with the phase.
    info, debug, _ = _capture(monkeypatch)
    LogSubscriber().handle_sync(AgentLifecycleEvent(session_id="zzzz", phase="evicted"))
    assert debug == []
    assert len(info) == 1 and "agent_lifecycle" in info[0] and "evicted" in info[0]


@pytest.mark.asyncio
async def test_async_handle_ignores_agent_lifecycle(monkeypatch):
    # Lifecycle rides the sync fan-out; the async path must not double-log it.
    info, debug, _ = _capture(monkeypatch)
    out = await LogSubscriber().handle(AgentLifecycleEvent(session_id="abcd1234ef", phase="added"))
    assert out is None
    assert info == [] and debug == []


def test_agent_lifecycle_via_emit_sync(monkeypatch):
    info, _, _ = _capture(monkeypatch)
    telemetry = InlineTelemetry(LogSubscriber())
    with bind_telemetry(telemetry):
        observe_event_sync(AgentLifecycleEvent(session_id="feedface", phase="added", detail="Foo"))
    assert any("agent_lifecycle" in line and "added" in line for line in info)
