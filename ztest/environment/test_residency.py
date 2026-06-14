#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Residency — ported from residency_tests.rs with injected fakes.

The codex tests run against a full ThreadManager; here we inject a fake
live-runtime map (a dict) plus real ``AgentRuntime`` + ``ResidencyStore`` so the
LRU/unload/rehydrate logic is exercised without the rust session machinery. We
test with ``capacity == 1`` to force eviction (codex reaches the same state via
``effective_agent_max_threads`` reserving a slot for the root thread).
"""

import types

import pytest

from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.exceptions import AgentLimitReached
from metagpt.environment.residency import Residency, ResidencySlot
from metagpt.environment.runtime import AgentRuntime, AgentStatus
from metagpt.environment.store import ResidencyStore


class FakeRole:
    def __init__(self, session_id):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())

    @property
    def session_id(self):
        return self._session_id

    def dump(self):
        return {"session_id": self._session_id}


def fake_role_loader(role_dump):
    return FakeRole(role_dump.get("session_id", "?"))


class LiveMap:
    """A stand-in for ThreadManagerState's live thread map."""

    def __init__(self):
        self.runtimes = {}
        self.removed = []

    def add(self, runtime):
        self.runtimes[runtime.session_id] = runtime

    def lookup(self, session_id):
        return self.runtimes.get(session_id)

    def remove(self, session_id):
        self.removed.append(session_id)
        self.runtimes.pop(session_id, None)


def make_runtime(session_id, *, status=AgentStatus.IDLE):
    rt = AgentRuntime(FakeRole(session_id))
    rt.status = status
    return rt


@pytest.fixture
def live():
    return LiveMap()


@pytest.fixture
def residency(live, tmp_path):
    return Residency(
        live.lookup,
        store=ResidencyStore(base_dir=str(tmp_path)),
        remove_runtime=live.remove,
    )


@pytest.mark.asyncio
async def test_reservation_unloads_oldest_idle_agent(live, residency, tmp_path):
    # capacity 1: reserve + commit worker-1 (idle/completed -> unloadable)
    slot1 = await residency.reserve_slot(1)
    assert isinstance(slot1, ResidencySlot)
    w1 = make_runtime("worker-1", status=AgentStatus.COMPLETED)
    live.add(w1)
    slot1.commit("worker-1")
    assert residency.residents() == ["worker-1"]

    # second reservation must evict worker-1 to make room
    slot2 = await residency.reserve_slot(1)
    assert "worker-1" in live.removed
    assert live.lookup("worker-1") is None
    # materialized to disk
    assert ResidencyStore(base_dir=str(tmp_path)).has("worker-1")

    w2 = make_runtime("worker-2", status=AgentStatus.COMPLETED)
    live.add(w2)
    slot2.commit("worker-2")
    assert residency.residents() == ["worker-2"]
    assert live.lookup("worker-2") is not None


@pytest.mark.asyncio
async def test_interrupted_agent_is_unloadable(live, residency):
    slot1 = await residency.reserve_slot(1)
    w1 = make_runtime("worker-1", status=AgentStatus.INTERRUPTED)
    live.add(w1)
    slot1.commit("worker-1")

    slot2 = await residency.reserve_slot(1)  # interrupted is final -> unloadable
    assert "worker-1" in live.removed
    slot2.rollback()


@pytest.mark.asyncio
async def test_running_agent_is_not_evicted(live, residency):
    slot1 = await residency.reserve_slot(1)
    w1 = make_runtime("worker-1", status=AgentStatus.RUNNING)  # not final
    live.add(w1)
    slot1.commit("worker-1")

    with pytest.raises(AgentLimitReached) as exc:
        await residency.reserve_slot(1)
    assert exc.value.max_threads == 1
    # not evicted, restored as resident
    assert "worker-1" not in live.removed
    assert residency.residents() == ["worker-1"]


@pytest.mark.asyncio
async def test_active_turn_blocks_eviction(live, residency):
    slot1 = await residency.reserve_slot(1)
    w1 = make_runtime("worker-1", status=AgentStatus.COMPLETED)
    w1.active_turn = True  # in-flight -> not unloadable
    live.add(w1)
    slot1.commit("worker-1")

    with pytest.raises(AgentLimitReached):
        await residency.reserve_slot(1)
    assert "worker-1" not in live.removed


@pytest.mark.asyncio
async def test_protected_agent_is_skipped(live, residency):
    # Two residents under capacity 2; protect worker-1, evict worker-2.
    slot1 = await residency.reserve_slot(2)
    live.add(make_runtime("worker-1", status=AgentStatus.COMPLETED))
    slot1.commit("worker-1")
    slot2 = await residency.reserve_slot(2)
    live.add(make_runtime("worker-2", status=AgentStatus.COMPLETED))
    slot2.commit("worker-2")
    assert residency.residents() == ["worker-1", "worker-2"]

    slot3 = await residency.reserve_slot(2, protected_session_id="worker-1")
    assert "worker-2" in live.removed
    assert "worker-1" not in live.removed
    slot3.rollback()


@pytest.mark.asyncio
async def test_rollback_releases_pending_slot(residency):
    slot = await residency.reserve_slot(1)
    assert residency.pending_slots == 1
    slot.rollback()
    assert residency.pending_slots == 0
    # capacity freed: can reserve again
    slot2 = await residency.reserve_slot(1)
    slot2.rollback()


@pytest.mark.asyncio
async def test_context_manager_rolls_back(residency):
    async with await residency.reserve_slot(1):
        assert residency.pending_slots == 1
    assert residency.pending_slots == 0


@pytest.mark.asyncio
async def test_touch_promotes_to_mru(live, residency):
    slot1 = await residency.reserve_slot(3)
    live.add(make_runtime("a", status=AgentStatus.COMPLETED))
    slot1.commit("a")
    slot2 = await residency.reserve_slot(3)
    live.add(make_runtime("b", status=AgentStatus.COMPLETED))
    slot2.commit("b")
    assert residency.residents() == ["a", "b"]
    residency.touch("a")
    assert residency.residents() == ["b", "a"]


@pytest.mark.asyncio
async def test_unbounded_capacity_never_evicts(live, residency):
    for name in ("a", "b", "c"):
        slot = await residency.reserve_slot(None)
        live.add(make_runtime(name, status=AgentStatus.COMPLETED))
        slot.commit(name)
    assert residency.residents() == ["a", "b", "c"]
    assert live.removed == []


@pytest.mark.asyncio
async def test_missing_runtime_is_dropped_during_unload(live, residency):
    # Resident recorded but absent from the live map. Faithful to codex: the
    # scan pops + drops the ghost (no push-back) but reports no eviction, so the
    # first reservation still raises. The deque is now empty, so a retry wins.
    slot1 = await residency.reserve_slot(1)
    slot1.commit("ghost")  # never added to live map
    assert residency.residents() == ["ghost"]

    with pytest.raises(AgentLimitReached):
        await residency.reserve_slot(1)
    assert residency.residents() == []  # ghost dropped

    slot2 = await residency.reserve_slot(1)  # room now available
    live.add(make_runtime("real", status=AgentStatus.COMPLETED))
    slot2.commit("real")
    assert residency.residents() == ["real"]
