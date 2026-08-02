#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Residency — ported from residency_tests.rs with injected fakes.

The codex tests run against a full ThreadManager; here we inject a fake
live-runtime map (a dict) plus real ``AgentRuntime`` + ``ResidencyStore`` so the
LRU/unload/rehydrate logic is exercised without the rust session machinery. We
test with ``capacity == 1`` to force eviction (codex reaches the same state via
``effective_agent_max_threads`` reserving a slot for the root thread).
"""

import asyncio
import types

import pytest

from mote.contracts.agent.errors import AgentLimitReached
from mote.contracts.content import ContentDigest
from mote.contracts.conversation import MessageQueue, UserMessage
from mote.contracts.events.envelope import JsonValue
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus
from mote.orchestration.agents.residency.lifecycle import (
    ResidentLifecyclePhase,
    ResidentPurgeAuthorization,
    ResidentTransitionDisposition,
)
from mote.orchestration.agents.residency.manager import Residency, ResidencySlot
from mote.orchestration.agents.residency.model import ResidencyIdentity
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.session import SessionLog
from mote.runtime.session.events import SessionMetaEvent

DIGEST = ContentDigest("b" * 64)


class FakeRole:
    def __init__(self, session_id):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())

    @property
    def session_id(self):
        return self._session_id

    def dump(self):
        return {"session_id": self._session_id}

    @property
    def residency_definition_id(self):
        return "fake.agent.v1"

    @property
    def residency_config_digest(self):
        return DIGEST

    def export_residency_state(self, *, session_history_is_durable: bool):
        return {"session_id": self.session_id}

    def restore_residency_message_buffer(self, snapshot: JsonValue):
        return None

    def restore_residency_history(self, messages, session_meta):
        return None


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
    leases = InMemoryLeaseCoordinator()
    sessions = tmp_path / "sessions"

    def authority(runtime):
        log = SessionLog(runtime.session_id, base_dir=str(sessions))
        if not log.exists():
            log.commit_offline(SessionMetaEvent(runtime.session_id, "fake.agent.v1", ()))
        identity = ResidencyIdentity(
            runtime.session_id,
            "root-1",
            "root-1",
            f"/root/{runtime.session_id}",
            runtime.session_id,
            "fake.agent.v1",
            DIGEST,
            1,
        )
        lease = leases.acquire(f"agent-residency:{runtime.session_id}", "owner-1", 30)
        return identity, lease

    return Residency(
        live.lookup,
        store=ResidencyStore(
            base_dir=str(tmp_path),
            sessions_base_dir=str(sessions),
            lease_coordinator=leases,
        ),
        remove_runtime=live.remove,
        materialization_authority=authority,
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
    assert (tmp_path / "worker-1.json").is_file()

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
    assert exc.value.limit == 1
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
async def test_try_reserve_sync_under_cap_bumps_pending(residency):
    assert residency.try_reserve_sync(2) is not None
    assert residency.pending_slots == 1
    assert residency.try_reserve_sync(2) is not None
    assert residency.pending_slots == 2


@pytest.mark.asyncio
async def test_try_reserve_sync_at_cap_returns_none(live, residency):
    slot = await residency.reserve_slot(1)
    live.add(make_runtime("a", status=AgentStatus.COMPLETED))
    slot.commit("a")  # one resident, cap 1
    assert residency.try_reserve_sync(1) is None
    assert residency.pending_slots == 0


@pytest.mark.asyncio
async def test_try_reserve_sync_unbounded_always_returns_slot(residency):
    assert residency.try_reserve_sync(None) is not None
    assert residency.try_reserve_sync(None) is not None
    assert residency.pending_slots == 2


@pytest.mark.asyncio
async def test_sync_slot_commit_turns_pending_resident(residency):
    slot = residency.try_reserve_sync(2)
    assert slot is not None
    slot.commit("x")
    assert residency.pending_slots == 0
    assert residency.residents() == ["x"]


@pytest.mark.asyncio
async def test_sync_slot_rollback_frees_reservation(residency):
    slot = residency.try_reserve_sync(2)
    assert slot is not None
    slot.rollback()
    assert residency.pending_slots == 0


@pytest.mark.asyncio
async def test_missing_runtime_is_dropped_during_unload(live, residency):
    # A resident absent from the canonical live map is atomically settled as
    # evicted, so the same reservation may consume the released capacity.
    slot1 = await residency.reserve_slot(1)
    slot1.commit("ghost")  # never added to live map
    assert residency.residents() == ["ghost"]

    slot2 = await residency.reserve_slot(1)
    assert residency.residents() == []  # ghost dropped

    live.add(make_runtime("real", status=AgentStatus.COMPLETED))
    slot2.commit("real")
    assert residency.residents() == ["real"]


@pytest.mark.asyncio
async def test_slow_eviction_keeps_capacity_and_fences_delivery(live, residency, monkeypatch):
    slot = await residency.reserve_slot(1)
    runtime = make_runtime("slow", status=AgentStatus.COMPLETED)
    live.add(runtime)
    slot.commit("slow")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_materialize(*args, **kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(residency.store, "materialize", blocking_materialize)
    reserving = asyncio.create_task(residency.reserve_slot(1))
    await started.wait()
    assert residency.residents() == ["slow"]
    assert residency.pending_slots == 0
    assert residency.try_reserve_sync(1) is None
    assert residency.runtime_for_delivery("slow") is None
    assert residency.lifecycle_snapshot("slow").phase is ResidentLifecyclePhase.EVICTING
    release.set()
    replacement = await reserving
    assert residency.residents() == []
    assert live.lookup("slow") is None
    replacement.rollback()


@pytest.mark.asyncio
async def test_delivery_after_snapshot_before_shutdown_cannot_enter_old_mailbox(live, residency, monkeypatch):
    slot = await residency.reserve_slot(1)
    runtime = make_runtime("snapshot", status=AgentStatus.COMPLETED)
    live.add(runtime)
    slot.commit("snapshot")
    shutdown_started = asyncio.Event()
    finish_shutdown = asyncio.Event()

    async def blocking_shutdown():
        shutdown_started.set()
        await finish_shutdown.wait()

    monkeypatch.setattr(runtime, "shutdown", blocking_shutdown)
    reserving = asyncio.create_task(residency.reserve_slot(1))
    await shutdown_started.wait()
    assert residency.store.has("snapshot")
    delivered = residency.deliver_if_active(
        "snapshot",
        lambda target: target.mailbox.enqueue(UserMessage("late")),
    )
    assert delivered is None
    assert runtime.mailbox.empty()
    finish_shutdown.set()
    replacement = await reserving
    replacement.rollback()


@pytest.mark.asyncio
async def test_failed_eviction_keeps_slot_in_retry_state(live, residency, monkeypatch):
    slot = await residency.reserve_slot(1)
    runtime = make_runtime("retry", status=AgentStatus.COMPLETED)
    live.add(runtime)
    slot.commit("retry")

    canonical_materialize = residency.store.materialize

    async def failed_materialize(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(residency.store, "materialize", failed_materialize)
    with pytest.raises(AgentLimitReached):
        await residency.reserve_slot(1)
    assert residency.residents() == ["retry"]
    assert residency.try_reserve_sync(1) is None
    assert residency.lifecycle_snapshot("retry").phase is ResidentLifecyclePhase.EVICTION_RETRY
    monkeypatch.setattr(residency.store, "materialize", canonical_materialize)
    assert await residency.retry_eviction("retry") is True
    assert residency.lifecycle_snapshot("retry").phase is ResidentLifecyclePhase.EVICTED
    assert live.lookup("retry") is None


def test_rehydration_claim_is_singleflight_and_stale_claim_cannot_overwrite(residency):
    residency.ensure_evicted("agent", 4)
    claims = [residency.begin_rehydration("agent") for _ in range(10)]
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    aborted = residency.abort_rehydration(claim)
    assert aborted.disposition is ResidentTransitionDisposition.FAILED_RETRYABLE
    stale = residency.complete_rehydration(claim, next_generation=5)
    assert stale.disposition is ResidentTransitionDisposition.STALE
    assert residency.lifecycle_snapshot("agent").phase is ResidentLifecyclePhase.EVICTED


def test_worker_loss_termination_tombstone_and_purge_are_distinct(residency):
    residency.register_active("agent", 2)
    active = residency.lifecycle_snapshot("agent")
    lost = residency.mark_worker_lost("agent", expected_generation=2, expected_revision=active.revision)
    assert lost.snapshot.phase is ResidentLifecyclePhase.LOST
    terminating = residency.begin_termination("agent", expected_generation=2, expected_revision=lost.snapshot.revision)
    terminal = residency.complete_termination(
        "agent", expected_generation=2, expected_revision=terminating.snapshot.revision
    )
    tombstone = residency.tombstone("agent", expected_generation=2, expected_revision=terminal.snapshot.revision)
    held = residency.purge(
        "agent",
        expected_generation=2,
        expected_revision=tombstone.snapshot.revision,
        authorization=ResidentPurgeAuthorization(True, True, True, True, legal_hold=True),
    )
    assert held.disposition is ResidentTransitionDisposition.REJECTED_GUARD
    purged = residency.purge(
        "agent",
        expected_generation=2,
        expected_revision=tombstone.snapshot.revision,
        authorization=ResidentPurgeAuthorization(True, True, True, True),
    )
    assert [
        lost.snapshot.phase,
        terminating.snapshot.phase,
        terminal.snapshot.phase,
        tombstone.snapshot.phase,
        purged.snapshot.phase,
    ] == [
        ResidentLifecyclePhase.LOST,
        ResidentLifecyclePhase.TERMINATING,
        ResidentLifecyclePhase.TERMINAL,
        ResidentLifecyclePhase.TOMBSTONED,
        ResidentLifecyclePhase.PURGED,
    ]
