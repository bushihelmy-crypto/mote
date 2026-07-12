#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for EventDrivenScheduler — turn-atomic delivery + bounded pump."""

import asyncio
import types

import pytest

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.mailbox import DeliveryMode
from metagpt.environment.runtime import AgentRuntime, AgentStatus
from metagpt.environment.turn_scheduler import EventDrivenScheduler


class FakeRole:
    """Drains its staged msg_buffer each turn; optional mid-turn hook."""

    def __init__(self, session_id="s1"):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        self.observed_turns = []  # one list[str] of drained contents per turn
        self.on_run = None  # optional async hook(role)
        self.runtime = None  # back-ref set by tests for mid-turn injection

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        if self.on_run is not None:
            await self.on_run(self)
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def make_runtime(session_id="s1"):
    role = FakeRole(session_id)
    rt = AgentRuntime(role)
    role.runtime = rt
    return rt


@pytest.mark.asyncio
async def test_run_executes_one_turn_for_woken_runtime():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("hello"))

    turns = await sched.run(1)
    assert turns == 1
    assert rt.role.observed_turns == [["hello"]]
    assert rt.status == AgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_queue_only_does_not_wake():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("later"), mode=DeliveryMode.QUEUE_ONLY)

    turns = await sched.run(1)
    assert turns == 0  # not ready -> no turn
    assert rt.role.observed_turns == []
    assert not rt.mailbox.empty()  # still queued


@pytest.mark.asyncio
async def test_mid_turn_queue_only_is_deferred():
    sched = EventDrivenScheduler()
    rt = make_runtime()

    async def inject_queue_only(role):
        # mid-turn: enqueue a queue-only item (must NOT be seen this turn)
        role.runtime.mailbox.enqueue(UserMessage("mid"), mode=DeliveryMode.QUEUE_ONLY)

    rt.role.on_run = inject_queue_only
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("first"))

    await sched.run(5)
    # only the first turn ran; mid-turn queue-only mail deferred (not delivered)
    assert rt.role.observed_turns == [["first"]]
    assert not rt.mailbox.empty()


@pytest.mark.asyncio
async def test_mid_turn_trigger_turn_earns_another_turn():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    fired = {"n": 0}

    async def inject_once(role):
        if fired["n"] == 0:
            fired["n"] += 1
            role.runtime.mailbox.enqueue(UserMessage("again"), mode=DeliveryMode.TRIGGER_TURN)
            role.runtime.wake()

    rt.role.on_run = inject_once
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("first"))

    turns = await sched.run(5)
    assert turns == 2
    assert rt.role.observed_turns == [["first"], ["again"]]


@pytest.mark.asyncio
async def test_run_is_bounded_by_k():
    sched = EventDrivenScheduler()
    rt = make_runtime()

    async def always_retrigger(role):
        role.runtime.mailbox.enqueue(UserMessage("loop"), mode=DeliveryMode.TRIGGER_TURN)
        role.runtime.wake()

    rt.role.on_run = always_retrigger
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("start"))

    turns = await sched.run(3)
    assert turns == 3  # bounded even though it would loop forever


@pytest.mark.asyncio
async def test_run_stops_early_when_quiescent():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    sched.add_runtime(rt)
    sched.notify("s1", UserMessage("one"))

    turns = await sched.run(10)
    assert turns == 1  # only one turn worth of work


@pytest.mark.asyncio
async def test_quiescent_reflects_pending_work():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    sched.add_runtime(rt)
    assert sched.quiescent()
    sched.notify("s1", UserMessage("x"))
    assert not sched.quiescent()
    await sched.run(10)
    assert sched.quiescent()


@pytest.mark.asyncio
async def test_notify_unknown_session_returns_false():
    sched = EventDrivenScheduler()
    assert sched.notify("nope", UserMessage("x")) is False


@pytest.mark.asyncio
async def test_multiple_runtimes_each_run_once_per_round():
    sched = EventDrivenScheduler()
    a, b = make_runtime("a"), make_runtime("b")
    sched.add_runtime(a)
    sched.add_runtime(b)
    sched.notify("a", UserMessage("ma"))
    sched.notify("b", UserMessage("mb"))

    turns = await sched.run(1)
    assert turns == 2
    assert a.role.observed_turns == [["ma"]]
    assert b.role.observed_turns == [["mb"]]


@pytest.mark.asyncio
async def test_persistent_driver_runs_turn_on_wake():
    sched = EventDrivenScheduler()
    rt = make_runtime()
    sched.add_runtime(rt)
    sched.start()
    try:
        sched.notify("s1", UserMessage("driven"))
        # let the driver task pick it up
        for _ in range(50):
            await asyncio.sleep(0.01)
            if rt.role.observed_turns:
                break
        assert rt.role.observed_turns == [["driven"]]
    finally:
        await sched.stop()
    assert rt.stopped is True
