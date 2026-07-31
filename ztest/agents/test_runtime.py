#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentRuntime — the live CodexThread analogue."""

import asyncio
import types

import pytest

from mote.contracts.conversation import MessageQueue, UserMessage
from mote.orchestration.agents.lifecycle.runtime import FINAL_STATUSES, AgentRuntime, AgentStatus, is_final
from mote.orchestration.agents.messaging.mailbox import Mailbox


class FakeRole:
    """Duck-typed Role: ``session_id``, ``run()``, ``dump()``, ``state.msg_buffer``."""

    def __init__(self, session_id="sess-1", *, behavior=None):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        self._behavior = behavior  # async callable(with_message) or None
        self.run_calls = []

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        self.run_calls.append(with_message)
        if self._behavior is not None:
            return await self._behavior(with_message)
        return f"ran:{with_message}"

    def dump(self):
        return {"session_id": self._session_id}


def test_is_final_classifies_statuses():
    assert FINAL_STATUSES == frozenset(
        {
            AgentStatus.COMPLETED,
            AgentStatus.REJECTED,
            AgentStatus.ERRORED,
            AgentStatus.INTERRUPTED,
        }
    )
    assert is_final(AgentStatus.COMPLETED)
    assert is_final(AgentStatus.REJECTED)
    assert is_final(AgentStatus.ERRORED)
    assert is_final(AgentStatus.INTERRUPTED)
    assert not is_final(AgentStatus.IDLE)
    assert not is_final(AgentStatus.RUNNING)
    assert not is_final(AgentStatus.NOT_FOUND)


def test_defaults():
    role = FakeRole()
    rt = AgentRuntime(role)
    assert rt.session_id == "sess-1"
    assert rt.status == AgentStatus.IDLE
    assert rt.active_turn is False
    assert isinstance(rt.mailbox, Mailbox)
    assert rt.msg_buffer is role.state.msg_buffer
    assert rt.stopped is False


def test_wake_sets_event():
    rt = AgentRuntime(FakeRole())
    assert not rt.wake_event.is_set()
    rt.wake()
    assert rt.wake_event.is_set()


@pytest.mark.asyncio
async def test_run_one_turn_completes():
    role = FakeRole()
    rt = AgentRuntime(role)
    rsp = await rt.run_one_turn()
    assert rsp == "ran:None"
    assert rt.status == AgentStatus.COMPLETED
    assert rt.active_turn is False
    assert role.run_calls == [None]


@pytest.mark.asyncio
async def test_run_one_turn_passes_message():
    role = FakeRole()
    rt = AgentRuntime(role)
    rsp = await rt.run_one_turn(with_message="hi")
    assert rsp == "ran:hi"
    assert role.run_calls == ["hi"]
    assert rt.status == AgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_run_one_turn_preserves_rejected_outcome_and_status():
    from mote.contracts.output import RunRejected, RunRejectionKind, TranscriptRef

    async def reject(_):
        return RunRejected(
            kind=RunRejectionKind.PROMPT_ADMISSION,
            reason="denied",
            transcript=TranscriptRef(session_id="sess-1"),
        )

    rt = AgentRuntime(FakeRole(behavior=reject))
    result = await rt.run_one_turn(with_message="blocked")

    assert isinstance(result, RunRejected)
    assert rt.last_run_result is result
    assert rt.status == AgentStatus.REJECTED


@pytest.mark.asyncio
async def test_run_one_turn_errored():
    async def boom(_):
        raise RuntimeError("explode")

    rt = AgentRuntime(FakeRole(behavior=boom))
    with pytest.raises(RuntimeError):
        await rt.run_one_turn()
    assert rt.status == AgentStatus.ERRORED
    assert rt.active_turn is False


@pytest.mark.asyncio
async def test_run_one_turn_interrupted():
    async def hang(_):
        await asyncio.sleep(10)

    rt = AgentRuntime(FakeRole(behavior=hang))
    task = asyncio.create_task(rt.run_one_turn())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rt.status == AgentStatus.INTERRUPTED
    assert rt.active_turn is False


def test_is_unloadable_requires_final_status():
    rt = AgentRuntime(FakeRole())
    assert not rt.is_unloadable()  # IDLE
    rt.status = AgentStatus.COMPLETED
    assert rt.is_unloadable()


def test_is_unloadable_false_with_active_turn():
    rt = AgentRuntime(FakeRole())
    rt.status = AgentStatus.COMPLETED
    rt.active_turn = True
    assert not rt.is_unloadable()


def test_is_unloadable_false_with_pending_mailbox():
    rt = AgentRuntime(FakeRole())
    rt.status = AgentStatus.COMPLETED
    rt.mailbox.enqueue(UserMessage("x"))
    assert not rt.is_unloadable()


def test_is_unloadable_false_with_pending_buffer():
    role = FakeRole()
    rt = AgentRuntime(role)
    rt.status = AgentStatus.COMPLETED
    role.state.msg_buffer.push(UserMessage("y"))
    assert not rt.is_unloadable()


@pytest.mark.asyncio
async def test_shutdown_cancels_task():
    async def hang(_):
        await asyncio.sleep(100)

    rt = AgentRuntime(FakeRole(behavior=hang))
    rt.task = asyncio.create_task(rt.run_one_turn())
    await asyncio.sleep(0.01)
    await rt.shutdown()
    assert rt.stopped is True
    assert rt.task is None
    assert rt.wake_event.is_set()


@pytest.mark.asyncio
async def test_shutdown_no_task_is_safe():
    rt = AgentRuntime(FakeRole())
    await rt.shutdown()
    assert rt.stopped is True
