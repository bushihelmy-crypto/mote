#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the EventBus spine — the two-plane dispatch model.

The bus fans every event through two phases:

* **control** (phase 1): subscribers exposing ``handle_control`` are routed by
  the event names they ``handles`` into per-event buckets, awaited inline in
  ``ControlStage`` order, and their typed :class:`ControlOutcome`\\s are folded
  (via each outcome's ``merge``) into the value ``emit`` returns. Only this plane
  can veto/mutate/stop. ``emit`` returns ``None`` when no control subscriber maps
  the event.
* **observation** (phase 2): subscribers exposing ``handle`` are fanned out,
  isolated and graded by ``delivery`` policy; their return is structurally
  dropped — an observer can never influence the fold.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from mote.common.events import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    EventBus,
    LLMStreamDeltaEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    ToolCallOutcome,
    ToolResultOutcome,
    current_bus,
    observe_event,
    observe_event_sync,
    set_bus,
)
from mote.common.interface.event_subscriber import (
    DURABLE,
    BusAware,
    ControlStage,
    ControlSubscriber,
    ObservationSubscriber,
    SyncObserver,
)


class ObserverSub(ObservationSubscriber):
    """An observation subscriber: records what it sees; its return is ignored."""

    def __init__(self, priority: int, log: list, *, tag: str = ""):
        self.priority = priority
        self._log = log
        self._tag = tag

    async def handle(self, event) -> None:
        self._log.append((self._tag or self.priority, event.name))


class ControlSub(ControlSubscriber):
    """A control subscriber: folds a fixed typed outcome via ``handle_control``.

    Declares the event names it ``handles`` (the bus routes by name into buckets);
    ``stage`` orders it within a shared bucket (rewrite before gate). The
    class-level ``handles`` is a non-empty placeholder so ``__init_subclass__``
    is satisfied; each instance overrides it via ``self.handles`` (the bus reads
    the instance attribute, which shadows the class default).
    """

    handles = (PRE_TOOL_USE,)

    def __init__(
        self,
        log: list,
        *,
        handles: tuple[str, ...],
        outcome=None,
        tag: str = "",
        stage: ControlStage = ControlStage.GATE,
    ):
        self.handles = handles
        self.stage = stage
        self._log = log
        self._outcome = outcome
        self._tag = tag

    async def handle_control(self, event):
        self._log.append((self._tag, event.name))
        return self._outcome


class SyncSub(ObservationSubscriber, SyncObserver):
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
    ctrl = ControlSub(log, handles=(PRE_TOOL_USE,), tag="ctrl")
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


def test_on_subscribed_hook_receives_bus():
    """A producer-observer's ``on_subscribed`` is called once with the bus."""

    class ProducerSub(ObservationSubscriber, BusAware):
        priority = 50

        def __init__(self):
            self.bus = None

        async def handle(self, event) -> None:
            return None

        def on_subscribed(self, bus) -> None:
            self.bus = bus

    bus = EventBus()
    sub = ProducerSub()
    bus.subscribe(sub)
    assert sub.bus is bus


def test_subscribe_without_on_subscribed_is_noop():
    """A subscriber lacking ``on_subscribed`` subscribes fine (no attr error)."""
    bus = EventBus()
    obs = ObserverSub(10, [], tag="plain")
    bus.subscribe(obs)  # must not raise
    assert obs in bus.subscribers


def test_unsubscribe_removes_from_either_plane():
    bus = EventBus()
    log: list = []
    ctrl = ControlSub(log, handles=(PRE_TOOL_USE,))
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
async def test_control_runs_before_observers():
    """Phase 1 (control) always precedes phase 2 (observers)."""
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(5, log, tag="obs"))
    bus.subscribe(ControlSub(log, handles=(PRE_TOOL_USE,), tag="ctrl"))
    await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert log == [("ctrl", "pre_tool_use"), ("obs", "pre_tool_use")]


def test_shared_bucket_orders_by_stage():
    """Two control subscribers on the same event run rewrite-before-gate,
    regardless of subscribe() order."""
    bus = EventBus()
    log: list = []
    gate = ControlSub(log, handles=(PRE_TOOL_USE,), tag="gate", stage=ControlStage.GATE)
    rewrite = ControlSub(log, handles=(PRE_TOOL_USE,), tag="rewrite", stage=ControlStage.REWRITE)
    bus.subscribe(gate)  # subscribed first, but runs second (gate stage)
    bus.subscribe(rewrite)
    assert bus.subscribers == [rewrite, gate]


# ---------------------------------------------------------------------------
# Fold (control plane only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_folds_deny_over_allow():
    bus = EventBus()
    log: list = []
    bus.subscribe(
        ControlSub(
            log,
            handles=(PRE_TOOL_USE,),
            outcome=ToolCallOutcome(behavior="allow"),
            stage=ControlStage.REWRITE,
        )
    )
    bus.subscribe(
        ControlSub(
            log,
            handles=(PRE_TOOL_USE,),
            outcome=ToolCallOutcome(behavior="deny"),
            stage=ControlStage.GATE,
        )
    )
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.behavior == "deny"


@pytest.mark.asyncio
async def test_emit_accumulates_additional_context():
    bus = EventBus()
    log: list = []
    bus.subscribe(
        ControlSub(
            log,
            handles=(POST_TOOL_USE,),
            outcome=ToolResultOutcome(additional_context=["a"]),
            stage=ControlStage.REWRITE,
        )
    )
    bus.subscribe(
        ControlSub(
            log,
            handles=(POST_TOOL_USE,),
            outcome=ToolResultOutcome(additional_context=["b"]),
            stage=ControlStage.GATE,
        )
    )
    out = await bus.emit(PostToolUseEvent(tool_name="Read", tool_response="x"))
    assert out.additional_context == ["a", "b"]


@pytest.mark.asyncio
async def test_observer_return_is_dropped_never_folded():
    """An observer that (wrongly) returns an outcome cannot influence the fold —
    only the control plane folds, by construction. With no control subscriber
    mapping the event, ``emit`` returns ``None``."""

    class SneakyObserver(ObservationSubscriber):
        priority = 10

        async def handle(self, event):
            return ToolCallOutcome(behavior="deny")  # structurally ignored

    bus = EventBus()
    bus.subscribe(SneakyObserver())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out is None  # the observer's "deny" never reaches the fold


@pytest.mark.asyncio
async def test_emit_with_no_control_returns_none():
    bus = EventBus()
    log: list = []
    bus.subscribe(ObserverSub(10, log))  # observer only
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert out is None


# ---------------------------------------------------------------------------
# Output-rewrite threading (PostToolUse ``updated_response``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_tool_use_output_rewrite_threads_forward():
    """A subscriber's ``updated_response`` is threaded to the next subscriber via
    the event's generic ``rewrite`` — the output-side twin of ``updated_args``
    threading — and the bus stamps *who* rewrote it (``by`` = the subscriber's
    ``name``) onto the event as provenance."""
    seen: list = []
    provenance: list = []

    class Rewriter(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE
        name = "redactor"

        async def handle_control(self, event):
            return ToolResultOutcome(updated_response="rewritten")

    class Observer(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.GATE

        async def handle_control(self, event):
            seen.append(event.tool_response)  # sees the already-rewritten text
            provenance.append(event.rewrites)  # and the recorded provenance
            return None

    bus = EventBus()
    bus.subscribe(Rewriter())
    bus.subscribe(Observer())
    out = await bus.emit(PostToolUseEvent(tool_name="Read", tool_response="original"))
    assert seen == ["rewritten"]
    assert out.updated_response == "rewritten"
    # The threaded event records the rewrite with its before-image and author.
    (recorded,) = provenance
    assert len(recorded) == 1
    assert recorded[0].field == "tool_response"
    assert recorded[0].before == "original"
    assert recorded[0].after == "rewritten"
    assert recorded[0].by == "redactor"  # bus stamped the subscriber's name


@pytest.mark.asyncio
async def test_output_rewrite_folds_last_wins():
    """When two subscribers both rewrite the output, the last one wins the fold
    (mirrors ``updated_args`` last-wins)."""

    class First(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE

        async def handle_control(self, event):
            return ToolResultOutcome(updated_response="first")

    class Second(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.GATE

        async def handle_control(self, event):
            return ToolResultOutcome(updated_response="second")

    bus = EventBus()
    bus.subscribe(First())
    bus.subscribe(Second())
    out = await bus.emit(PostToolUseEvent(tool_name="Read", tool_response="orig"))
    assert out.updated_response == "second"


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_observer_does_not_break_stream():
    bus = EventBus()
    log: list = []

    class Boom(ObservationSubscriber):
        priority = 5

        async def handle(self, event):
            raise RuntimeError("boom")

    bus.subscribe(Boom())
    bus.subscribe(ObserverSub(10, log, tag="ok"))
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("ok", "llm_stream_delta")]
    assert out is None


@pytest.mark.asyncio
async def test_one_bad_control_subscriber_does_not_break_fold():
    bus = EventBus()
    log: list = []

    class BoomControl(ControlSubscriber):
        handles = (PRE_TOOL_USE,)
        stage = ControlStage.REWRITE  # runs first, fails open (default)

        async def handle_control(self, event):
            raise RuntimeError("boom")

    bus.subscribe(BoomControl())
    bus.subscribe(
        ControlSub(
            log,
            handles=(PRE_TOOL_USE,),
            outcome=ToolCallOutcome(behavior="deny"),
            tag="ok",
            stage=ControlStage.GATE,
        )
    )
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    # The raising control sub (fail-open) is skipped; the good one still folds.
    assert out.behavior == "deny"
    assert log == [("ok", "pre_tool_use")]


# ---------------------------------------------------------------------------
# Timeout circuit breakers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_subscriber_timeout_is_skipped():
    class Hang(ControlSubscriber):
        handles = (PRE_TOOL_USE,)

        async def handle_control(self, event):
            await asyncio.sleep(10)
            return ToolCallOutcome(behavior="deny")

    bus = EventBus(control_timeout=0.01)
    bus.subscribe(Hang())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    # Timed out → fail-open (default) → no outcome, never freezes the spine.
    assert out is None


@pytest.mark.asyncio
async def test_mirror_observer_timeout_is_dropped():
    log: list = []

    class Slow(ObservationSubscriber):
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
    class DurableBoom(ObservationSubscriber):
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

    class SlowDurable(ObservationSubscriber):
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
    bus.subscribe(ControlSub(log, handles=(PRE_TOOL_USE,), outcome=ToolCallOutcome(behavior="deny"), tag="ctrl"))
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

    class BoomSync(ObservationSubscriber, SyncObserver):
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

    class ContextProbe(ControlSubscriber):
        handles = (PRE_TOOL_USE,)

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


# ---------------------------------------------------------------------------
# Contract enforcement — "declare, don't sniff" fails loud
# ---------------------------------------------------------------------------


def test_control_subclass_with_empty_handles_fails_at_class_def():
    """A control subscriber that forgets to declare ``handles`` is a routing
    no-op — caught at class-definition time, not silently mapped to nothing."""
    with pytest.raises(TypeError, match="handles"):

        class NoHandles(ControlSubscriber):
            handles = ()  # empty — the bus could never route to it

            async def handle_control(self, event):
                return None


def test_fail_closed_subclass_without_on_failure_fails_at_class_def():
    """A fail-closed gate must define ``on_failure`` so the bus can synthesize a
    typed deny when it crashes; omitting it is caught at class-def time."""
    from mote.common.interface.event_subscriber import FAIL_CLOSED

    with pytest.raises(TypeError, match="on_failure"):

        class BadGate(ControlSubscriber):
            handles = (PRE_TOOL_USE,)
            fail_mode = FAIL_CLOSED

            async def handle_control(self, event):
                return None


def test_control_subscriber_missing_handle_control_cannot_instantiate():
    """A control subscriber without ``handle_control`` is abstract — the missing
    method is caught at construction, not when the bus first dispatches to it."""

    class NoHandler(ControlSubscriber):
        handles = (PRE_TOOL_USE,)

    with pytest.raises(TypeError):
        NoHandler()


def test_observation_subscriber_missing_handle_cannot_instantiate():
    class NoHandle(ObservationSubscriber):
        pass

    with pytest.raises(TypeError):
        NoHandle()


def test_sync_observer_missing_handle_sync_cannot_instantiate():
    class NoSync(ObservationSubscriber, SyncObserver):
        async def handle(self, event):
            return None

    with pytest.raises(TypeError):
        NoSync()


def test_bus_aware_missing_on_subscribed_cannot_instantiate():
    class NoHook(ObservationSubscriber, BusAware):
        async def handle(self, event):
            return None

    with pytest.raises(TypeError):
        NoHook()


def test_subscribe_non_subscriber_raises_type_error():
    """An object that declares neither plane is rejected at ``subscribe`` — the
    bus never guesses a plane from method names."""

    class Bystander:
        async def handle(self, event):  # right method name, no plane declared
            return None

    bus = EventBus()
    with pytest.raises(TypeError, match="neither a ControlSubscriber"):
        bus.subscribe(Bystander())


@pytest.mark.asyncio
async def test_wrong_type_outcome_is_contained_fail_open():
    """A control subscriber that returns an outcome of the wrong type for the
    event does NOT crash the turn — a fail-open sub's malformed outcome is
    dropped (routed through ``fail_mode`` exactly like a handler crash)."""

    class WrongType(ControlSubscriber):
        handles = (PRE_TOOL_USE,)

        async def handle_control(self, event):
            # PreToolUse binds ToolCallOutcome; returning a ToolResultOutcome is
            # the wrong type and must be contained, not folded.
            return ToolResultOutcome(updated_response="nope")

    bus = EventBus()
    bus.subscribe(WrongType())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out is None  # contained (fail-open drops it), turn survives


@pytest.mark.asyncio
async def test_wrong_type_outcome_is_contained_fail_closed_denies():
    """A fail-closed gate whose (buggy) outcome is the wrong type is contained
    per ``fail_mode`` — its ``on_failure`` typed deny folds, the turn survives."""
    from mote.common.interface.event_subscriber import FAIL_CLOSED

    class WrongTypeGate(ControlSubscriber):
        handles = (PRE_TOOL_USE,)
        fail_mode = FAIL_CLOSED

        async def handle_control(self, event):
            return ToolResultOutcome(updated_response="nope")  # wrong type

        def on_failure(self, reason: str) -> ToolCallOutcome:
            return ToolCallOutcome(behavior="deny", system_message=reason)

    bus = EventBus()
    bus.subscribe(WrongTypeGate())
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out is not None and out.behavior == "deny"
