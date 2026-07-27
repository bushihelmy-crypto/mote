#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ChildAgentHandle — the unified spawn→run→release wrapper."""

import types

import pytest

from mote.contracts.output import CommittedOutput, RunResult, TranscriptRef
from mote.contracts.schema.queue import MessageQueue
from mote.orchestration.environment.agent_path import AgentPath
from mote.orchestration.environment.handle import ChildAgentHandle
from mote.orchestration.environment.runtime import AgentRuntime, AgentStatus


class FakeRole:
    def __init__(self, session_id, *, summary="done"):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        committed = CommittedOutput("candidate", "mote.text@1", "sha", summary)
        self.run_result = RunResult(
            output=summary,
            output_record=committed,
            transcript=TranscriptRef(session_id=session_id),
        )
        self.cleaned = False
        self.turns = []

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        self.turns.append(with_message)
        return self.run_result

    async def cleanup(self):
        self.cleaned = True


class FakeControl:
    """Minimal control stub recording which children were released."""

    def __init__(self):
        self.released = []

    def release_child(self, agent_id):
        self.released.append(agent_id)


class FakeSlot:
    """Minimal residency slot stub recording rollback calls."""

    def __init__(self):
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1


def _handle(summary="done", *, residency_slot=None, timeout_seconds=None):
    role = FakeRole("child-1", summary=summary)
    runtime = AgentRuntime(role, agent_path=AgentPath.from_string("/root/child"))
    control = FakeControl()
    h = ChildAgentHandle(
        runtime,
        control=control,
        agent_id="child-1",
        agent_path=AgentPath.from_string("/root/child"),
        residency_slot=residency_slot,
        timeout_seconds=timeout_seconds,
    )
    return h, role, control


@pytest.mark.asyncio
async def test_run_to_completion_returns_summary_and_releases():
    h, role, control = _handle(summary="  the summary  ")
    out = await h.run_to_completion("go")
    assert out.output == "  the summary  "
    assert role.turns == ["go"]
    assert control.released == ["child-1"]
    assert role.cleaned is True


@pytest.mark.asyncio
async def test_run_to_completion_releases_even_on_error():
    h, role, control = _handle()

    async def boom(with_message=None):
        raise RuntimeError("kaboom")

    role.run = boom
    with pytest.raises(RuntimeError):
        await h.run_to_completion("go")
    # slot released + role cleaned up despite the failure
    assert control.released == ["child-1"]
    assert role.cleaned is True


@pytest.mark.asyncio
async def test_aclose_is_idempotent():
    h, role, control = _handle()
    await h.aclose()
    await h.aclose()
    assert control.released == ["child-1"]  # released exactly once


@pytest.mark.asyncio
async def test_async_with_releases_on_exit():
    h, role, control = _handle()
    async with h as handle:
        assert handle is h
    assert control.released == ["child-1"]
    assert role.cleaned is True


@pytest.mark.asyncio
async def test_join_waits_for_final_status():
    h, role, control = _handle()
    h.runtime.status = AgentStatus.COMPLETED
    status = await h.join()
    assert status == AgentStatus.COMPLETED


def test_accessors_expose_identity():
    h, role, control = _handle(summary="hi")
    assert h.session_id == "child-1"
    assert h.agent_path == AgentPath.from_string("/root/child")
    assert h.result is None
    assert h.runtime is not None


@pytest.mark.asyncio
async def test_ephemeral_handle_releases_slot_on_aclose():
    slot = FakeSlot()
    h, role, control = _handle(residency_slot=slot)
    await h.aclose()
    assert slot.rolled_back == 1
    assert control.released == ["child-1"]


@pytest.mark.asyncio
async def test_ephemeral_handle_releases_slot_on_error_path():
    slot = FakeSlot()
    h, role, control = _handle(residency_slot=slot)

    async def boom(with_message=None):
        raise RuntimeError("kaboom")

    role.run = boom
    with pytest.raises(RuntimeError):
        await h.run_to_completion("go")
    # the live-incarnation slot is freed even though the run blew up
    assert slot.rolled_back == 1
    assert control.released == ["child-1"]


@pytest.mark.asyncio
async def test_ephemeral_slot_released_exactly_once():
    slot = FakeSlot()
    h, role, control = _handle(residency_slot=slot)
    await h.aclose()
    await h.aclose()
    assert slot.rolled_back == 1  # idempotent — released exactly once


@pytest.mark.asyncio
async def test_timeout_returns_partial_summary_and_releases():
    """A turn that outlasts ``timeout_seconds`` is cut short; the handle returns
    whatever summary the role had (soft failure) and still recycles the slot."""
    import asyncio

    slot = FakeSlot()
    h, role, control = _handle(summary="partial so far", residency_slot=slot, timeout_seconds=0.02)

    async def slow(with_message=None):
        await asyncio.sleep(10)  # far past the 0.02s budget

    role.run = slow
    out = await h.run_to_completion("go")
    assert out is None
    assert slot.rolled_back == 1  # recycled despite the timeout
    assert control.released == ["child-1"]
    assert role.cleaned is True


@pytest.mark.asyncio
async def test_no_timeout_runs_to_completion_unbounded():
    """Without a timeout the turn runs to natural completion (no wait_for)."""
    h, role, control = _handle(summary="full result", timeout_seconds=None)
    out = await h.run_to_completion("go")
    assert out.output == "full result"
    assert role.turns == ["go"]


@pytest.mark.asyncio
async def test_fast_turn_under_timeout_completes_normally():
    """A turn well under budget completes normally and returns its full summary."""
    h, role, control = _handle(summary="quick result", timeout_seconds=5.0)
    out = await h.run_to_completion("go")
    assert out.output == "quick result"
    assert role.turns == ["go"]
    assert control.released == ["child-1"]
