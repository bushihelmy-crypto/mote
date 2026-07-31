#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`ConnectionScope` — the per-connection presentation edge.

The scope is the multi-session dual of ``SessionDriver.run()``: it owns a
per-connection ``BaseProjector`` subscribed to a resident Role's telemetry,
drives one turn against the shared control plane, and tears its edge down on
close (leaving the engine running). The headline invariant: **two concurrent
scopes over two sessions have independent, non-interleaving event streams** —
an event on one session's telemetry reaches only that scope's consumers.

A lightweight ``FakeTelemetry`` fans an event out to its handlers (mirroring the
real observation plane's ``handle``), so folding through the real
``ViewProjector`` + ``CapabilityAdapter`` into a recording consumer is exercised
end-to-end without a real engine.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from mote.contracts.events.conversation import MESSAGE_APPENDED
from mote.product.presentation.consumer import BaseConsumer
from mote.product.presentation.events import Capabilities, MessageBlockCompleted
from mote.product.session_hosting.connection import ConnectionScope, _format_turn_error
from mote.product.session_hosting.registry import ResidentSession


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _FakeHandle:
    def __init__(self, telemetry: "FakeTelemetry", handler: Any) -> None:
        self._telemetry = telemetry
        self._handler = handler

    async def aclose(self) -> None:
        if self._handler in self._telemetry.handlers:
            self._telemetry.handlers.remove(self._handler)


class FakeTelemetry:
    """A minimal observation plane that fans events out to handlers."""

    def __init__(self) -> None:
        self.handlers: List[Any] = []

    async def subscribe(self, binding: Any) -> _FakeHandle:
        self.handlers.append(binding.handler)
        return _FakeHandle(self, binding.handler)

    async def emit(self, event: Any) -> None:
        for handler in list(self.handlers):
            await handler.handle(event)


class FakeAgentEvt:
    """Duck-typed AgentEvent the ViewProjector folds (name + payload)."""

    def __init__(self, name: str, **fields: Any) -> None:
        self.name = name
        for k, v in fields.items():
            setattr(self, k, v)


def ev_message(role: str, content: str) -> FakeAgentEvt:
    return FakeAgentEvt(MESSAGE_APPENDED, message=SimpleNamespace(role=role, content=content))


class FakeRole:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.telemetry = FakeTelemetry()


class FakeRuntime:
    def __init__(self, last_error: Optional[BaseException] = None) -> None:
        self.last_error = last_error


class FakeControl:
    """Records inputs; drives quiescence via a scripted sequence."""

    def __init__(self, runtime: Optional[FakeRuntime] = None, quiescent_seq: Optional[List[bool]] = None) -> None:
        self.inputs: List[Any] = []
        self._runtime = runtime if runtime is not None else FakeRuntime()
        self._quiescent_seq = list(quiescent_seq) if quiescent_seq is not None else None

    def send_input(self, agent_id: str, message: Any) -> None:
        self.inputs.append((agent_id, message))

    def quiescent(self) -> bool:
        if self._quiescent_seq:
            return self._quiescent_seq.pop(0)
        return True

    def get_runtime(self, agent_id: str):
        return self._runtime


class RecordingConsumer(BaseConsumer):
    """Captures every ViewEvent that reaches it (post capability-adapter)."""

    def __init__(self, caps: Optional[Capabilities] = None) -> None:
        self.capabilities = caps or Capabilities(streaming=True, markdown=True)
        self.events: List[Any] = []
        self.closed = False

    async def handle(self, ev: Any) -> None:
        self.events.append(ev)

    def handle_sync(self, ev: Any) -> None:
        self.events.append(ev)

    async def aclose(self) -> None:
        self.closed = True


def make_session(session_id: str, *, quiescent_seq=None, last_error=None) -> ResidentSession:
    role = FakeRole(session_id)
    control = FakeControl(FakeRuntime(last_error=last_error), quiescent_seq=quiescent_seq)
    return ResidentSession(session_id=session_id, control=control, role=role, agent_id=session_id)


def _texts(consumer: RecordingConsumer) -> List[str]:
    """Markdown of every MessageBlockCompleted that reached the consumer."""
    return [e.markdown for e in consumer.events if isinstance(e, MessageBlockCompleted)]


# --------------------------------------------------------------------------
# Lifecycle: subscribe / bind / teardown
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_open_subscribes_projector_to_role_telemetry():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    await scope.open()
    assert scope.projector in session.role.telemetry.handlers
    await scope.aclose()
    assert scope.projector not in session.role.telemetry.handlers


@pytest.mark.asyncio
async def test_aclose_closes_consumers():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    await scope.open()
    await scope.aclose()
    assert consumer.closed is True


@pytest.mark.asyncio
async def test_port_bound_and_restored():
    session = make_session("s1")
    prior = object()
    session.role.state.env = prior
    port = SimpleNamespace()
    scope = ConnectionScope(session, consumers=[], port=port)
    await scope.open()
    # A human channel wrapping the port is now bound.
    assert session.role.state.env is not prior
    await scope.aclose()
    assert session.role.state.env is prior  # restored on close


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes():
    session = make_session("s1")
    consumer = RecordingConsumer()
    async with ConnectionScope(session, consumers=[consumer]) as scope:
        assert scope.projector in session.role.telemetry.handlers
    assert scope.projector not in session.role.telemetry.handlers
    assert consumer.closed is True


# --------------------------------------------------------------------------
# run_turn
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_turn_surfaces_user_message_then_sends_input():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    await scope.open()
    msg = SimpleNamespace(content="hello there", id="m-1")
    await scope.run_turn(msg)
    # The user's own turn is surfaced as a completed block on the consumer.
    assert "hello there" in _texts(consumer)
    # And delivered to the control plane against the right agent id.
    assert session.control.inputs == [("s1", msg)]
    await scope.aclose()


@pytest.mark.asyncio
async def test_run_turn_polls_until_quiescent():
    # Not quiescent for two polls, then quiescent — run_turn must wait it out.
    session = make_session("s1", quiescent_seq=[False, False, True])
    scope = ConnectionScope(session, consumers=[RecordingConsumer()], quiescent_poll_interval=0.0)
    await scope.open()
    await scope.run_turn(SimpleNamespace(content="x", id="m"))
    assert session.control._quiescent_seq == []  # sequence fully consumed
    await scope.aclose()


@pytest.mark.asyncio
async def test_run_turn_errored_surfaces_error_raised():
    from mote.product.presentation.events import ErrorRaised

    session = make_session("s1", last_error=RuntimeError("boom"))
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    await scope.open()
    await scope.run_turn(SimpleNamespace(content="x", id="m"))
    errs = [e for e in consumer.events if isinstance(e, ErrorRaised)]
    assert len(errs) == 1
    assert "boom" in errs[0].text
    await scope.aclose()


def test_format_turn_error_plain_and_status():
    assert _format_turn_error(RuntimeError("oops")) == "RuntimeError: oops"
    err = RuntimeError("bad")
    err.status_code = 503  # type: ignore[attr-defined]
    assert _format_turn_error(err) == "RuntimeError (HTTP 503): bad"


# --------------------------------------------------------------------------
# THE headline invariant: two concurrent scopes never interleave streams
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_scopes_have_isolated_event_streams():
    """Two sessions, two scopes: an event on one bus reaches only its consumer."""
    sess_a = make_session("a")
    sess_b = make_session("b")
    consumer_a = RecordingConsumer()
    consumer_b = RecordingConsumer()
    scope_a = ConnectionScope(sess_a, consumers=[consumer_a])
    scope_b = ConnectionScope(sess_b, consumers=[consumer_b])
    await scope_a.open()
    await scope_b.open()

    # Emit an assistant message on EACH bus, concurrently.
    await asyncio.gather(
        sess_a.role.telemetry.emit(ev_message("assistant", "reply-A")),
        sess_b.role.telemetry.emit(ev_message("assistant", "reply-B")),
    )

    # Each consumer saw ONLY its own session's assistant reply — no cross-talk.
    assert _texts(consumer_a) == ["reply-A"]
    assert _texts(consumer_b) == ["reply-B"]

    await scope_a.aclose()
    await scope_b.aclose()


@pytest.mark.asyncio
async def test_session_resident_across_multiple_turns():
    """A resident session drives many turns; each turn reuses the same control."""
    session = make_session("s1")
    scope = ConnectionScope(session, consumers=[RecordingConsumer()])
    await scope.open()
    await scope.run_turn(SimpleNamespace(content="turn-1", id="m1"))
    await scope.run_turn(SimpleNamespace(content="turn-2", id="m2"))
    # Both turns landed on the SAME resident control plane, in order.
    assert [m.content for _aid, m in session.control.inputs] == ["turn-1", "turn-2"]
    await scope.aclose()
