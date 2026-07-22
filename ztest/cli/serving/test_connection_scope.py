#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`ConnectionScope` — the per-connection presentation edge.

The scope is the multi-session dual of ``SessionDriver.run()``: it owns a
per-connection ``BaseProjector`` subscribed to a resident session's role bus,
drives one turn against the shared control plane, and tears its edge down on
close (leaving the engine running). The headline invariant: **two concurrent
scopes over two sessions have independent, non-interleaving event streams** —
an event on one session's bus reaches only that scope's consumers.

A lightweight ``FakeBus`` fans an event out to its subscribers (mirroring the
real observation plane's ``handle``), so folding through the real
``ViewProjector`` + ``CapabilityAdapter`` into a recording consumer is exercised
end-to-end without a real engine.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from mote.cli.contracts.base import BaseConsumer
from mote.cli.contracts.view import Capabilities, MessageBlockCompleted
from mote.cli.serving.connection_scope import ConnectionScope, _format_turn_error
from mote.cli.serving.session_registry import ResidentSession
from mote.common.events.types import MESSAGE_APPENDED


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeBus:
    """A minimal observation plane: fan an event out to subscribers' ``handle``."""

    def __init__(self) -> None:
        self.subscribers: List[Any] = []

    def subscribe(self, sub: Any) -> None:
        self.subscribers.append(sub)

    def unsubscribe(self, sub: Any) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    async def emit(self, event: Any) -> None:
        for sub in list(self.subscribers):
            await sub.handle(event)


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
        self.event_bus = FakeBus()


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
async def test_open_subscribes_projector_to_role_bus():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    scope.open()
    assert scope.projector in session.role.event_bus.subscribers
    await scope.aclose()
    assert scope.projector not in session.role.event_bus.subscribers  # unsubscribed


@pytest.mark.asyncio
async def test_aclose_closes_consumers():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    scope.open()
    await scope.aclose()
    assert consumer.closed is True


@pytest.mark.asyncio
async def test_port_bound_and_restored():
    session = make_session("s1")
    prior = object()
    session.role.state.env = prior
    port = SimpleNamespace()
    scope = ConnectionScope(session, consumers=[], port=port)
    scope.open()
    # A human channel wrapping the port is now bound.
    assert session.role.state.env is not prior
    await scope.aclose()
    assert session.role.state.env is prior  # restored on close


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes():
    session = make_session("s1")
    consumer = RecordingConsumer()
    async with ConnectionScope(session, consumers=[consumer]) as scope:
        assert scope.projector in session.role.event_bus.subscribers
    assert scope.projector not in session.role.event_bus.subscribers
    assert consumer.closed is True


# --------------------------------------------------------------------------
# run_turn
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_turn_surfaces_user_message_then_sends_input():
    session = make_session("s1")
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    scope.open()
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
    scope.open()
    await scope.run_turn(SimpleNamespace(content="x", id="m"))
    assert session.control._quiescent_seq == []  # sequence fully consumed
    await scope.aclose()


@pytest.mark.asyncio
async def test_run_turn_errored_surfaces_error_raised():
    from mote.cli.contracts.view import ErrorRaised

    session = make_session("s1", last_error=RuntimeError("boom"))
    consumer = RecordingConsumer()
    scope = ConnectionScope(session, consumers=[consumer])
    scope.open()
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
    scope_a.open()
    scope_b.open()

    # Emit an assistant message on EACH bus, concurrently.
    await asyncio.gather(
        sess_a.role.event_bus.emit(ev_message("assistant", "reply-A")),
        sess_b.role.event_bus.emit(ev_message("assistant", "reply-B")),
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
    scope.open()
    await scope.run_turn(SimpleNamespace(content="turn-1", id="m1"))
    await scope.run_turn(SimpleNamespace(content="turn-2", id="m2"))
    # Both turns landed on the SAME resident control plane, in order.
    assert [m.content for _aid, m in session.control.inputs] == ["turn-1", "turn-2"]
    await scope.aclose()
