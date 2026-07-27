from __future__ import annotations

import asyncio

import pytest

from mote.contracts.errors.runtimes import (
    LeaseFencedError,
    LeaseUnavailableError,
    ManagedRuntimeAliasConflictError,
    ManagedRuntimeDurabilityError,
    ManagedRuntimeNotFoundError,
    ManagedRuntimeRevisionConflictError,
    ManagedRuntimeStateError,
)
from mote.contracts.leases import LeasePolicy
from mote.contracts.ports import LiveSurfaceRuntimeDriver, ManagedRuntimeDriver
from mote.contracts.runtimes import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeDurabilityState,
    RuntimeHealth,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
    RuntimeState,
)
from mote.runtime.interactive import RuntimeHost
from mote.runtime.interactive.checkpoint_codec import decode_inline_json, encode_inline_json
from mote.runtime.leases import FileLeaseCoordinator, InMemoryLeaseCoordinator
from mote.runtime.tools.dependency._kernel import KernelRuntimeDriver
from mote.runtime.tools.dependency._terminal import TerminalRuntimeDriver


class FakeDriver:
    kind = "fake"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.FULL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"text"}),
        multi_instance=True,
    )

    def __init__(self) -> None:
        self.started_with = None
        self.start_calls = 0
        self.closed = False
        self.value = 0
        self.checkpoint_calls = 0

    async def start(self, checkpoint=None):
        self.start_calls += 1
        self.started_with = checkpoint
        return DriverStartResult(restored=checkpoint is not None)

    async def health(self):
        return RuntimeHealth(healthy=not self.closed)

    async def checkpoint(self, reason: str):
        self.checkpoint_calls += 1
        return DriverCheckpoint(
            codec="fake@1",
            schema_version=1,
            payload_ref=f"memory:{self.value}:{reason}",
            digest=f"value-{self.value}",
        )

    async def aclose(self):
        self.closed = True


class FakeCheckpointSink:
    def __init__(self) -> None:
        self.items = []

    async def persist(self, checkpoint, *, reason: str) -> None:
        self.items.append((checkpoint, reason))


class UnavailableCheckpointDriver(FakeDriver):
    async def checkpoint(self, reason: str):
        raise RuntimeError("checkpoint unavailable")


class SingleInstanceDriver(FakeDriver):
    capabilities = RuntimeCapabilities(multi_instance=False)


class FailingCheckpointSink:
    async def persist(self, checkpoint, *, reason: str) -> None:
        raise OSError("rollout unavailable")


class RecoveringCheckpointSink(FakeCheckpointSink):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def persist(self, checkpoint, *, reason: str) -> None:
        if self.fail:
            raise OSError("rollout unavailable")
        await super().persist(checkpoint, reason=reason)


class RecoveringPayloadStore:
    def __init__(self) -> None:
        self.fail = True

    async def seal(self, checkpoint):
        if self.fail:
            raise OSError("artifact store unavailable")
        return checkpoint

    async def open(self, checkpoint):
        return checkpoint


class ProjectionJournal:
    def __init__(self, *, fail_commit: bool = False, fail_ack: bool = False) -> None:
        self.fail_commit = fail_commit
        self.fail_ack = fail_ack
        self.facts = []
        self.acks = []

    async def record_commit(self, fact) -> None:
        if self.fail_commit:
            raise OSError("projection journal unavailable")
        self.facts.append(fact)

    async def acknowledge(self, ack) -> None:
        if self.fail_ack:
            raise OSError("projection ack unavailable")
        self.acks.append(ack)


def test_fake_driver_satisfies_runtime_protocol():
    assert isinstance(FakeDriver(), ManagedRuntimeDriver)


def test_real_stateful_drivers_satisfy_runtime_protocol():
    assert isinstance(TerminalRuntimeDriver(session_key="terminal", cwd=None), ManagedRuntimeDriver)
    assert isinstance(
        TerminalRuntimeDriver(session_key="terminal", cwd=None),
        LiveSurfaceRuntimeDriver,
    )
    assert isinstance(KernelRuntimeDriver(session_key="kernel", cwd=None), ManagedRuntimeDriver)
    assert isinstance(KernelRuntimeDriver(session_key="kernel", cwd=None), LiveSurfaceRuntimeDriver)


@pytest.mark.asyncio
async def test_create_resolve_checkpoint_and_close():
    host = RuntimeHost()
    driver = FakeDriver()
    created = await host.create(driver, alias="main", runtime_id="runtime-1")

    assert created.ref.readable == "fake:main"
    assert created.epoch == 1
    assert created.revision == 0
    assert created.state is RuntimeState.READY
    assert host.descriptor("fake:main") == created
    assert (await host.health(created.ref)).healthy

    checkpoint = await host.checkpoint(created.ref, reason="test")
    assert checkpoint.runtime_id == "runtime-1"
    assert checkpoint.codec == "fake@1"
    assert checkpoint.fidelity is CheckpointFidelity.FULL

    await host.close(created.ref)
    assert driver.closed
    with pytest.raises(ManagedRuntimeNotFoundError):
        host.descriptor("fake:main")


@pytest.mark.asyncio
async def test_non_multi_instance_driver_rejects_second_alias():
    host = RuntimeHost()
    await host.create(SingleInstanceDriver(), alias="first")

    with pytest.raises(ManagedRuntimeAliasConflictError, match="multiple instances"):
        await host.create(SingleInstanceDriver(), alias="second")


@pytest.mark.asyncio
async def test_explicit_checkpoint_is_persisted_through_host_sink():
    sink = FakeCheckpointSink()
    host = RuntimeHost(checkpoint_sink=sink)
    created = await host.create(FakeDriver(), runtime_id="runtime-1")

    checkpoint = await host.checkpoint(created.ref, reason="manual")

    assert sink.items == [(checkpoint, "manual")]


@pytest.mark.asyncio
async def test_changed_write_persists_final_revision_but_unchanged_write_does_not():
    sink = FakeCheckpointSink()
    host = RuntimeHost(checkpoint_sink=sink)
    created = await host.create(FakeDriver(), runtime_id="runtime-1")

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 1
        access.commit()

    assert len(sink.items) == 1
    assert sink.items[0][0].revision == 1
    assert sink.items[0][0].digest == "value-1"
    assert sink.items[0][1] == "write-commit"

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.commit(changed=False)

    assert len(sink.items) == 1


@pytest.mark.asyncio
async def test_unavailable_automatic_checkpoint_does_not_break_committed_write():
    sink = FakeCheckpointSink()
    host = RuntimeHost(checkpoint_sink=sink)
    created = await host.create(UnavailableCheckpointDriver(), runtime_id="runtime-1")

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 1
        access.commit()

    assert host.descriptor(created.ref).revision == 1
    assert sink.items == []
    health = await host.health(created.ref)
    assert health.durability is RuntimeDurabilityState.LAGGING
    assert health.current_revision == 1
    assert health.recoverable_revision == 0
    assert "checkpoint unavailable" in health.durability_detail
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_checkpoint_or_committed_write():
    host = RuntimeHost(checkpoint_sink=FailingCheckpointSink())
    created = await host.create(FakeDriver(), runtime_id="runtime-1")

    checkpoint = await host.checkpoint(created.ref, reason="manual")
    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 2
        access.commit()

    assert checkpoint.revision == 0
    assert host.descriptor(created.ref).revision == 1
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_checkpoint_health_recovers_after_a_later_successful_sink_write():
    sink = RecoveringCheckpointSink()
    durability_events = []
    host = RuntimeHost(
        checkpoint_sink=sink,
        durability_observer=durability_events.append,
    )
    created = await host.create(FakeDriver(), runtime_id="runtime-1")

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 1
        access.commit()

    failed = await host.health(created.ref)
    assert failed.durability is RuntimeDurabilityState.LAGGING
    assert failed.recoverable_revision == 0

    sink.fail = False
    for _ in range(100):
        if (await host.health(created.ref)).durability is RuntimeDurabilityState.CURRENT:
            break
        await asyncio.sleep(0.01)

    recovered = await host.health(created.ref)
    assert recovered.durability is RuntimeDurabilityState.CURRENT
    assert recovered.current_revision == recovered.recoverable_revision == 1
    assert recovered.durability_detail == ""
    assert host.descriptor(created.ref).durability is RuntimeDurabilityState.CURRENT
    assert [event.state for event in durability_events] == ["lagging", "current"]
    assert durability_events[0].current_revision == 1
    assert durability_events[0].recoverable_revision == 0
    assert "rollout unavailable" in durability_events[0].detail
    assert durability_events[1].recoverable_revision == 1
    assert durability_events[1].detail == ""
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_sealing_failure_is_observable_and_compensated():
    payloads = RecoveringPayloadStore()
    sink = FakeCheckpointSink()
    host = RuntimeHost(checkpoint_sink=sink, checkpoint_payload_store=payloads)
    created = await host.create(FakeDriver(), runtime_id="runtime-seal")

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 1
        access.commit()

    failed = await host.health(created.ref)
    assert failed.durability is RuntimeDurabilityState.LAGGING
    assert "artifact store unavailable" in failed.durability_detail

    payloads.fail = False
    for _ in range(100):
        if (await host.health(created.ref)).durability is RuntimeDurabilityState.CURRENT:
            break
        await asyncio.sleep(0.01)

    assert (await host.health(created.ref)).recoverable_revision == 1
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_projected_write_persists_single_required_commit_fact_and_ack():
    sink = FakeCheckpointSink()
    journal = ProjectionJournal()
    host = RuntimeHost(checkpoint_sink=sink, projection_journal=journal)
    created = await host.create(FakeDriver(), runtime_id="runtime-1")
    intent = RuntimeProjectionIntent(
        intent_id="artifact",
        projector="test-artifact",
        schema_version=1,
    )

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 3
        access.commit(projections=(intent,))

    assert access.result_commit_id is not None
    assert sink.items == []
    assert len(journal.facts) == 1
    fact = journal.facts[0]
    assert fact.commit_id == access.result_commit_id
    assert fact.checkpoint.revision == 1
    assert fact.projections == (intent,)

    assert await host.acknowledge_projection(fact.commit_id, "artifact") is True
    assert journal.acks == [RuntimeProjectionAck(commit_id=fact.commit_id, intent_id="artifact")]


@pytest.mark.asyncio
async def test_required_projection_fact_failure_marks_committed_runtime_degraded():
    journal = ProjectionJournal(fail_commit=True)
    durability_events = []
    host = RuntimeHost(
        projection_journal=journal,
        durability_observer=durability_events.append,
    )
    created = await host.create(FakeDriver(), runtime_id="runtime-1")
    intent = RuntimeProjectionIntent(
        intent_id="artifact",
        projector="test-artifact",
        schema_version=1,
    )

    with pytest.raises(ManagedRuntimeDurabilityError) as raised:
        async with host.access(created.ref, mode="write", owner_id="tool") as access:
            access.driver.value = 4
            access.commit(projections=(intent,))

    assert raised.value.context["revision"] == 1
    descriptor = host.descriptor(created.ref)
    assert descriptor.revision == 1
    assert descriptor.state is RuntimeState.DEGRADED
    assert descriptor.durability is RuntimeDurabilityState.LAGGING
    assert "projection journal unavailable" in descriptor.durability_detail
    assert [event.state for event in durability_events] == ["lagging"]
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_projection_fact_failure_is_compensated_in_current_process():
    journal = ProjectionJournal(fail_commit=True)
    durability_events = []
    host = RuntimeHost(
        projection_journal=journal,
        durability_observer=durability_events.append,
    )
    created = await host.create(FakeDriver(), runtime_id="runtime-1")
    intent = RuntimeProjectionIntent(
        intent_id="artifact",
        projector="test-artifact",
        schema_version=1,
    )

    with pytest.raises(ManagedRuntimeDurabilityError):
        async with host.access(created.ref, mode="write", owner_id="tool") as access:
            access.driver.value = 5
            access.commit(projections=(intent,))

    journal.fail_commit = False
    for _ in range(100):
        if (await host.health(created.ref)).durability is RuntimeDurabilityState.CURRENT:
            break
        await asyncio.sleep(0.01)

    descriptor = host.descriptor(created.ref)
    assert descriptor.state is RuntimeState.READY
    assert descriptor.durability is RuntimeDurabilityState.CURRENT
    assert descriptor.recoverable_revision == descriptor.revision == 1
    assert len(journal.facts) == 1
    assert journal.facts[0].projections == (intent,)
    assert [event.state for event in durability_events] == ["lagging", "current"]
    await host.close(created.ref)


@pytest.mark.asyncio
async def test_failed_projection_ack_is_safe_and_remains_replayable():
    journal = ProjectionJournal(fail_ack=True)
    host = RuntimeHost(projection_journal=journal)

    assert await host.acknowledge_projection("commit-1", "artifact") is False
    assert journal.acks == []


@pytest.mark.asyncio
async def test_host_without_sink_skips_automatic_checkpoint_work():
    driver = FakeDriver()
    host = RuntimeHost()
    created = await host.create(driver, runtime_id="runtime-1")

    async with host.access(created.ref, mode="write", owner_id="tool") as access:
        access.driver.value = 1
        access.commit()

    assert driver.checkpoint_calls == 0


@pytest.mark.asyncio
async def test_write_commits_one_revision_and_failed_context_does_not_commit():
    host = RuntimeHost()
    created = await host.create(FakeDriver(), runtime_id="runtime-1")

    async with host.access(created.ref, mode="write", owner_id="tool-1", expected_revision=0) as access:
        access.driver.value = 1
        assert access.commit() == 1
    assert access.result_revision == 1
    assert host.descriptor(created.ref).revision == 1

    with pytest.raises(RuntimeError):
        async with host.access(created.ref, mode="write", owner_id="tool-2", expected_revision=1) as access:
            access.driver.value = 2
            access.commit()
            raise RuntimeError("after mutation")

    descriptor = host.descriptor(created.ref)
    assert descriptor.revision == 1
    assert descriptor.state is RuntimeState.DEGRADED
    with pytest.raises(ManagedRuntimeStateError):
        async with host.access(created.ref, mode="write", owner_id="tool-3"):
            pass


@pytest.mark.asyncio
async def test_revision_conflict_is_detected_before_driver_access():
    host = RuntimeHost()
    created = await host.create(FakeDriver())
    with pytest.raises(ManagedRuntimeRevisionConflictError) as caught:
        async with host.access(created.ref, mode="write", owner_id="tool", expected_revision=9):
            raise AssertionError("must not enter")
    assert caught.value.context["current_revision"] == 0


@pytest.mark.asyncio
async def test_read_access_cannot_commit_mutation():
    host = RuntimeHost()
    created = await host.create(FakeDriver())
    async with host.access(created.ref, mode="read", owner_id="reader") as access:
        with pytest.raises(ManagedRuntimeStateError):
            access.commit()
    assert host.descriptor(created.ref).revision == 0


@pytest.mark.asyncio
async def test_concurrent_writers_are_serialized():
    host = RuntimeHost()
    created = await host.create(FakeDriver())
    order = []

    async def mutate(owner: str, delay: float):
        async with host.access(created.ref, mode="write", owner_id=owner) as access:
            order.append(f"{owner}:start")
            await asyncio.sleep(delay)
            access.driver.value += 1
            access.commit()
            order.append(f"{owner}:end")

    await asyncio.gather(mutate("a", 0.02), mutate("b", 0.0))
    assert order == ["a:start", "a:end", "b:start", "b:end"]
    assert host.descriptor(created.ref).revision == 2


@pytest.mark.asyncio
async def test_checkpoint_restore_advances_epoch_and_keeps_revision():
    checkpoint = RuntimeCheckpoint(
        runtime_id="runtime-1",
        kind="fake",
        epoch=4,
        revision=12,
        codec="fake@1",
        schema_version=1,
        payload_ref="memory:12",
        fidelity=CheckpointFidelity.FULL,
    )
    driver = FakeDriver()
    host = RuntimeHost()
    created = await host.create(driver, runtime_id="runtime-1", checkpoint=checkpoint)
    assert created.epoch == 5
    assert created.revision == 12
    assert driver.started_with == checkpoint


@pytest.mark.asyncio
async def test_staged_checkpoint_is_restored_once_by_lazy_ensure():
    checkpoint = RuntimeCheckpoint(
        runtime_id="runtime-staged",
        kind="fake",
        epoch=2,
        revision=7,
        codec="fake@1",
        schema_version=1,
        payload_ref="memory:7",
        fidelity=CheckpointFidelity.FULL,
    )
    host = RuntimeHost()
    host.stage_checkpoint(checkpoint)
    first = FakeDriver()

    created = await host.ensure(first)

    assert created.ref.runtime_id == "runtime-staged"
    assert created.epoch == 3
    assert created.revision == 7
    assert first.started_with == checkpoint

    await host.close(created.ref)
    second = FakeDriver()
    recreated = await host.ensure(second)
    assert recreated.ref.runtime_id != "runtime-staged"
    assert second.started_with is None


@pytest.mark.asyncio
async def test_alias_conflict_and_close_all_are_deterministic():
    host = RuntimeHost()
    first = FakeDriver()
    second = FakeDriver()
    await host.create(first, alias="one")
    await host.create(second, alias="two")
    with pytest.raises(ManagedRuntimeAliasConflictError):
        await host.create(FakeDriver(), alias="one")

    assert await host.close_all() == {}
    assert first.closed and second.closed
    assert host.list() == []


@pytest.mark.asyncio
async def test_concurrent_ensure_creates_one_runtime():
    host = RuntimeHost()
    first = FakeDriver()
    second = FakeDriver()

    created, existing = await asyncio.gather(host.ensure(first), host.ensure(second))

    assert created.ref == existing.ref
    assert len(host.list()) == 1
    assert first.start_calls + second.start_calls == 1


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("coordinator_factory", [InMemoryLeaseCoordinator, FileLeaseCoordinator])
def test_generic_lease_takeover_and_fencing(tmp_path, coordinator_factory):
    clock = Clock()
    if coordinator_factory is FileLeaseCoordinator:
        coordinator = coordinator_factory(tmp_path / "leases.json", clock=clock)
    else:
        coordinator = coordinator_factory(clock=clock)

    first = coordinator.acquire("runtime:r1", "agent", 10)
    with pytest.raises(LeaseUnavailableError):
        coordinator.acquire("runtime:r1", "human", 10)

    clock.now += 11
    second = coordinator.acquire("runtime:r1", "human", 10)
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(LeaseFencedError):
        coordinator.assert_current("runtime:r1", first.fencing_token)
    coordinator.assert_current("runtime:r1", second.fencing_token)


def test_lease_policy_rejects_unsafe_heartbeat_window():
    with pytest.raises(ValueError):
        LeasePolicy(ttl_seconds=5, renew_interval_seconds=5)


def test_inline_checkpoint_codec_round_trip_and_digest_guard():
    encoded = encode_inline_json(
        {"cwd": "/work", "env": {"A": "1"}},
        codec="test+json@1",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    checkpoint = RuntimeCheckpoint(
        runtime_id="runtime-1",
        kind="fake",
        epoch=1,
        revision=2,
        codec=encoded.codec,
        schema_version=encoded.schema_version,
        payload_ref=encoded.payload_ref,
        digest=encoded.digest,
        fidelity=encoded.fidelity or CheckpointFidelity.NONE,
    )
    assert decode_inline_json(checkpoint, codec="test+json@1") == {
        "cwd": "/work",
        "env": {"A": "1"},
    }

    tampered = RuntimeCheckpoint(
        runtime_id=checkpoint.runtime_id,
        kind=checkpoint.kind,
        epoch=checkpoint.epoch,
        revision=checkpoint.revision,
        codec=checkpoint.codec,
        schema_version=checkpoint.schema_version,
        payload_ref=checkpoint.payload_ref,
        digest="bad",
        fidelity=checkpoint.fidelity,
    )
    with pytest.raises(ValueError, match="digest"):
        decode_inline_json(tampered, codec="test+json@1")
