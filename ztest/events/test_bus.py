#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the EventBus spine — the two-plane dispatch model.

The bus fans every event through two phases:

* **control** (phase 1): subscribers exposing ``handle_control`` are awaited
  inline in priority order, and their :class:`HookOutcome`\\s are folded into the
  value ``emit`` returns. Only this plane can veto/mutate/stop.
* **observation** (phase 2): subscribers exposing ``handle`` are fanned out,
  isolated and graded by ``delivery`` policy; their return is structurally
  dropped — an observer can never influence the fold.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from metagpt.common.events import (
    EventBus,
    LLMStreamDeltaEvent,
    PreToolUseEvent,
    current_bus,
    observe_event,
    observe_event_sync,
    set_bus,
)
from metagpt.common.interface.event_subscriber import DURABLE
from metagpt.common.hook.types import HookOutcome


class ObserverSub:
    """An observation subscriber: records what it sees; its return is ignored."""

    def __init__(self, priority: int, log: list, *, tag: str = ""):
        self.priority = priority
        self._log = log
        self._tag = tag

    async def handle(self, event) -> None:
        self._log.append((self._tag or self.priority, event.name))


class ControlSub:
    """A control subscriber: folds a fixed outcome via ``handle_control``."""

    def __init__(self, priority: int, log: list, *, outcome: Optional[HookOutcome] = None, tag: str = ""):
        self.priority = priority
        self._log = log
        self._outcome = outcome
        self._tag = tag

    async def handle_control(self, event) -> Optional[HookOutcome]:
        self._log.append((self._tag or self.priority, event.name))
        return self._outcome


class SyncSub:
    def __init__(self, priority: int, log: list):
        self.priority = priority
        self._log = log

    async def handle(self, event) -> None:
        return None

    def handle_sync(self, event):
        self._log.append(("sync", event.name))


# ---------------------------------------------------------------------------
# Classification + ordering
# ---------------------------------------------------------------------------


def test_subscribe_classifies_control_vs_observers():
    bus = EventBus()
    log: list = []
    ctrl = ControlSub(10, log, tag="ctrl")
    obs = ObserverSub(50, log, tag="obs")
    bus.subscribe(obs)
    bus.subscribe(ctrl)
    # Dispatch order is control plane first, then observers.
    assert bus.subscribers == [ctrl, obs]


def test_subscribe_orders_observers_by_ascending_priority():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(50, log, tag="mid"))
    bus.subscribe(ObserverSub(10, log, tag="low"))
    bus.subscribe(ObserverSub(80, log, tag="high"))
    tags = [getattr(s, "_tag") for s in bus.subscribers]
    assert tags == ["low", "mid", "high"]


def test_equal_priority_keeps_insertion_order():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(10, log, tag="first"))
    bus.subscribe(ObserverSub(10, log, tag="second"))
    tags = [getattr(s, "_tag") for s in bus.subscribers]
    assert tags == ["first", "second"]


def test_unsubscribe_removes_from_either_plane():
    bus = EventBus()
    log: list = []
    ctrl = ControlSub(10, log)
    obs = ObserverSub(50, log)
    bus.subscribe(ctrl)
    bus.subscribe(obs)
    bus.unsubscribe(ctrl)
    bus.unsubscribe(obs)
    assert bus.subscribers == []
    bus.unsubscribe(obs)  # idempotent no-op


@pytest.mark.asyncio
async def test_emit_dispatches_observers_in_priority_order():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(50, log, tag="mid"))
    bus.subscribe(ObserverSub(10, log, tag="low"))
    await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("low", "llm_stream_delta"), ("mid", "llm_stream_delta")]


@pytest.mark.asyncio
async def test_control_runs_before_observers_regardless_of_priority():
    """Phase 1 (control) always precedes phase 2 (observers), even when the
    control subscriber's numeric priority is higher than an observer's."""
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(5, log, tag="obs"))  # lower number = earlier *within* plane
    bus.subscribe(ControlSub(90, log, tag="ctrl"))  # higher number, still runs first
    await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert log == [("ctrl", "pre_tool_use"), ("obs", "pre_tool_use")]


# ---------------------------------------------------------------------------
# Fold (control plane only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_folds_deny_over_allow():
    bus = EventBus()
    log: list = []
    bus.subscribe(ControlSub(10, log, outcome=HookOutcome(behavior="allow")))
    bus.subscribe(ControlSub(20, log, outcome=HookOutcome(behavior="deny")))
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.behavior == "deny"


@pytest.mark.asyncio
async def test_emit_accumulates_additional_context():
    bus = EventBus()
    log: list = []
    bus.subscribe(ControlSub(10, log, outcome=HookOutcome(additional_context=["a"])))
    bus.subscribe(ControlSub(20, log, outcome=HookOutcome(additional_context=["b"])))
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.additional_context == ["a", "b"]


@pytest.mark.asyncio
async def test_observer_return_is_dropped_never_folded():
    """An observer that (wrongly) returns an outcome cannot influence the fold —
    only the control plane folds, by construction."""

    class SneakyObserver:
        priority = 10

        async def handle(self, event):
            return HookOutcome(behavior="deny")  # structurally ignored

    bus = EventBus()
    bus.subscribe(SneakyObserver())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.behavior is None  # the observer's "deny" never reaches the fold


@pytest.mark.asyncio
async def test_emit_with_no_control_returns_empty():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(10, log))  # observer only
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert out.behavior is None
    assert not out.additional_context


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_observer_does_not_break_stream():
    bus = EventBus()
    log: list = []

    class Boom:
        priority = 5

        async def handle(self, event):
            raise RuntimeError("boom")

    bus.subscribe(Boom())
    bus.subscribe(ObserverSub(10, log, tag="ok"))
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("ok", "llm_stream_delta")]
    assert out.behavior is None


@pytest.mark.asyncio
async def test_one_bad_control_subscriber_does_not_break_fold():
    bus = EventBus()
    log: list = []

    class BoomControl:
        priority = 5

        async def handle_control(self, event):
            raise RuntimeError("boom")

    bus.subscribe(BoomControl())
    bus.subscribe(ControlSub(10, log, outcome=HookOutcome(behavior="deny"), tag="ok"))
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    # The raising control sub is skipped; the good one still folds.
    assert out.behavior == "deny"
    assert log == [("ok", "pre_tool_use")]


# ---------------------------------------------------------------------------
# Timeout circuit breakers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_subscriber_timeout_is_skipped():
    class Hang:
        priority = 5

        async def handle_control(self, event):
            await asyncio.sleep(10)
            return HookOutcome(behavior="deny")

    bus = EventBus(control_timeout=0.01)
    bus.subscribe(Hang())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    # Timed out → treated as no-outcome, never freezes the spine.
    assert out.behavior is None


@pytest.mark.asyncio
async def test_mirror_observer_timeout_is_dropped():
    log: list = []

    class Slow:
        priority = 5

        async def handle(self, event):
            await asyncio.sleep(10)
            log.append("slow")

    bus = EventBus(observer_timeout=0.01)
    bus.subscribe(Slow())
    bus.subscribe(ObserverSub(10, log, tag="fast"))
    await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("fast", "llm_stream_delta")]  # slow dropped, fast still ran


# ---------------------------------------------------------------------------
# Delivery policy (DURABLE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_sink_failure_is_counted_not_swallowed_silently():
    class DurableBoom:
        priority = 5
        delivery = DURABLE

        async def handle(self, event):
            raise RuntimeError("disk full")

    bus = EventBus()
    bus.subscribe(DurableBoom())
    assert bus.durable_failures == 0
    await bus.emit(LLMStreamDeltaEvent(token="x"))  # must not raise into the turn
    assert bus.durable_failures == 1


@pytest.mark.asyncio
async def test_durable_sink_is_not_time_boxed():
    """A durable sink that takes longer than the mirror budget still completes
    (it is never wrapped in wait_for)."""
    done: list = []

    class SlowDurable:
        priority = 5
        delivery = DURABLE

        async def handle(self, event):
            await asyncio.sleep(0.05)
            done.append("written")

    bus = EventBus(observer_timeout=0.01)
    bus.subscribe(SlowDurable())
    await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert done == ["written"]
    assert bus.durable_failures == 0


# ---------------------------------------------------------------------------
# observe() — phase-2 only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_runs_observers_but_not_control():
    bus = EventBus()
    log: list = []
    bus.subscribe(ControlSub(10, log, outcome=HookOutcome(behavior="deny"), tag="ctrl"))
    bus.subscribe(ObserverSub(20, log, tag="obs"))
    await bus.observe(PreToolUseEvent(tool_name="Bash"))
    # Only the observer ran; control plane is skipped on the observation path.
    assert log == [("obs", "pre_tool_use")]


# ---------------------------------------------------------------------------
# Sync emit
# ---------------------------------------------------------------------------


def test_emit_sync_only_reaches_sync_subscribers():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(10, log, tag="async-only"))  # no handle_sync
    bus.subscribe(SyncSub(20, log))
    bus.emit_sync(LLMStreamDeltaEvent(token="x"))
    assert log == [("sync", "llm_stream_delta")]


def test_emit_sync_isolates_errors():
    bus = EventBus()

    class BoomSync:
        priority = 5

        async def handle(self, event):
            return None

        def handle_sync(self, event):
            raise RuntimeError("boom")

    log: list = []
    bus.subscribe(BoomSync())
    bus.subscribe(SyncSub(10, log))
    bus.emit_sync(LLMStreamDeltaEvent(token="x"))  # must not raise
    assert log == [("sync", "llm_stream_delta")]


# ---------------------------------------------------------------------------
# Contextvar binding (observation transport)
# ---------------------------------------------------------------------------


def test_current_bus_unbound_is_none():
    assert current_bus() is None


@pytest.mark.asyncio
async def test_observe_event_no_op_without_bus():
    await observe_event(LLMStreamDeltaEvent(token="x"))  # no bus bound, must not raise


@pytest.mark.asyncio
async def test_observe_event_routes_to_bound_bus():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(10, log, tag="bound"))
    with set_bus(bus):
        assert current_bus() is bus
        await observe_event(LLMStreamDeltaEvent(token="tok"))
    assert current_bus() is None  # reset on exit
    assert log == [("bound", "llm_stream_delta")]


def test_observe_event_sync_no_op_without_bus():
    observe_event_sync(LLMStreamDeltaEvent(token="x"))  # must not raise


def test_observe_event_sync_routes_to_bound_bus():
    bus = EventBus()
    log: list = []
    bus.subscribe(SyncSub(10, log))
    with set_bus(bus):
        observe_event_sync(LLMStreamDeltaEvent(token="tok"))
    assert log == [("sync", "llm_stream_delta")]


# ---------------------------------------------------------------------------
# Inline-dispatch invariant (control plane)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_runs_in_callers_contextvar_scope():
    """The control phase must dispatch inline (same task), so a contextvar the
    caller sets is visible inside ``handle_control``. This is what lets a hook
    veto fold before the emitter proceeds. Moving control onto ``create_task``
    would run it in a fresh context and break the inline-fold guarantee."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    seen: list = []

    class ContextProbe:
        priority = 10

        async def handle_control(self, event):
            seen.append(marker.get())
            return None

    bus = EventBus()
    bus.subscribe(ContextProbe())
    token = marker.set("in-callers-context")
    try:
        await bus.emit(PreToolUseEvent(tool_name="Bash"))
    finally:
        marker.reset(token)
    assert seen == ["in-callers-context"]
