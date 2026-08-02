from __future__ import annotations

from dataclasses import replace

import pytest

from mote.contracts.ports.runtime.handoff import RuntimeHandoffJournal
from mote.contracts.runtime import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeHealth,
    RuntimeState,
)
from mote.contracts.runtime.handoff import RuntimeHandoffIntent
from mote.runtime.interactive.host import RuntimeHost
from mote.runtime.session import RuntimeCheckpointEvent, SessionLog, SessionMetaEvent
from mote.runtime.session.replay import replay
from mote.runtime.session.runtime_handoff import SessionRuntimeHandoffJournal


def _checkpoint(*, digest: str = "before", revision: int = 2) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        runtime_id="canvas-1",
        kind="canvas",
        alias="default",
        epoch=1,
        revision=revision,
        codec="canvas-document+json@1",
        schema_version=1,
        payload_ref=f"memory:{digest}",
        digest=digest,
        fidelity=CheckpointFidelity.FULL,
    )


def _intent() -> RuntimeHandoffIntent:
    checkpoint = _checkpoint()
    return RuntimeHandoffIntent(
        handoff_id="handoff-1",
        runtime_id=checkpoint.runtime_id,
        kind=checkpoint.kind,
        alias=checkpoint.alias,
        epoch=checkpoint.epoch,
        base_revision=checkpoint.revision,
        target_revision=checkpoint.revision + 1,
        owner_id="human:test",
        fencing_token=7,
        mode="exclusive",
        message="adjust the legend",
        selection=("legend",),
        base_checkpoint=checkpoint,
    )


async def _journal(tmp_path):
    log = SessionLog("runtime-handoff", base_dir=str(tmp_path))
    await log.append(SessionMetaEvent(session_id="runtime-handoff"))
    return log, SessionRuntimeHandoffJournal(log)


class RecoveringCanvasDriver:
    kind = "canvas"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.FULL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"canvas"}),
    )

    def __init__(self, *, fail_start: bool = False) -> None:
        self.started_with = None
        self.fail_start = fail_start

    async def start(self, checkpoint=None):
        self.started_with = checkpoint
        if self.fail_start:
            raise RuntimeError("driver start failed")
        return DriverStartResult(restored=checkpoint is not None)

    async def health(self):
        return RuntimeHealth(healthy=True)

    async def checkpoint(self, reason: str):
        return DriverCheckpoint(
            codec="canvas-document+json@1",
            schema_version=1,
            payload_ref="memory:restored",
            digest="restored",
        )

    async def aclose(self):
        return None


def test_session_runtime_handoff_journal_satisfies_port(tmp_path):
    journal = SessionRuntimeHandoffJournal(SessionLog("runtime-handoff-port", base_dir=str(tmp_path)))

    assert isinstance(journal, RuntimeHandoffJournal)


@pytest.mark.asyncio
async def test_prepared_handoff_is_replayed_then_reclaimed_without_activation(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)

    assert replay(log).pending_runtime_handoffs[intent.handoff_id].active is False

    recovery = await journal.recovery(
        kind="canvas",
        alias="default",
        checkpoint=None,
    )

    assert recovery.runtime_id == intent.runtime_id
    assert recovery.checkpoint == intent.base_checkpoint
    assert recovery.recovered_handoff_ids == (intent.handoff_id,)
    assert intent.handoff_id in replay(log).pending_runtime_handoffs


@pytest.mark.asyncio
async def test_active_handoff_preserves_durable_human_checkpoint_on_recovery(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)
    await journal.activate(intent.handoff_id)
    changed = _checkpoint(digest="after")
    await log.append(RuntimeCheckpointEvent(changed, reason="handoff-after"))

    recovery = await journal.recovery(
        kind="canvas",
        alias="default",
        checkpoint=None,
    )

    assert recovery.checkpoint == replace(changed, revision=intent.target_revision)
    state = replay(log)
    assert intent.handoff_id in state.pending_runtime_handoffs
    assert state.runtime_checkpoints["canvas:default"] == changed


@pytest.mark.asyncio
async def test_runtime_host_restores_identity_and_agent_ownership_after_crash(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)
    await journal.activate(intent.handoff_id)
    driver = RecoveringCanvasDriver()
    host = RuntimeHost(handoff_journal=journal)

    descriptor = await host.ensure(driver)

    assert descriptor.ref.runtime_id == intent.runtime_id
    assert descriptor.state is RuntimeState.READY
    assert descriptor.epoch == intent.epoch + 1
    assert descriptor.revision == intent.base_revision
    assert driver.started_with == intent.base_checkpoint
    state = replay(log)
    assert state.pending_runtime_handoffs == {}
    assert state.runtime_checkpoints["canvas:default"].epoch == descriptor.epoch
    await host.close_all()


@pytest.mark.asyncio
async def test_reclaim_ack_waits_for_start_and_preserves_checkpointless_identity(
    tmp_path,
):
    log, journal = await _journal(tmp_path)
    intent = replace(_intent(), base_checkpoint=None)
    await journal.prepare(intent)
    await journal.activate(intent.handoff_id)
    failed_host = RuntimeHost(handoff_journal=journal)

    with pytest.raises(RuntimeError, match="driver start failed"):
        await failed_host.ensure(RecoveringCanvasDriver(fail_start=True))

    assert intent.handoff_id in replay(log).pending_runtime_handoffs

    recovered_host = RuntimeHost(handoff_journal=journal)
    recovered = await recovered_host.ensure(RecoveringCanvasDriver())
    assert recovered.ref.runtime_id == intent.runtime_id
    assert recovered.epoch == intent.epoch + 1
    assert recovered.revision == intent.base_revision
    assert replay(log).pending_runtime_handoffs == {}
    await recovered_host.close_all()

    restarted_host = RuntimeHost(handoff_journal=journal)
    restarted = await restarted_host.ensure(RecoveringCanvasDriver())
    assert restarted.ref.runtime_id == intent.runtime_id
    assert restarted.epoch == recovered.epoch + 1
    assert restarted.revision == recovered.revision
    await restarted_host.close_all()
