from __future__ import annotations

import pytest

from mote.contracts.ports import RuntimeOperationJournal
from mote.contracts.runtimes import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionIntent,
)
from mote.runtime.session import RuntimeCommitEvent, SessionLog, SessionMetaEvent
from mote.runtime.session.replay import replay
from mote.runtime.session.runtime_operation import SessionRuntimeOperationJournal


def _checkpoint(revision: int = 2) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        runtime_id="canvas-1",
        kind="canvas",
        alias="default",
        epoch=1,
        revision=revision,
        codec="canvas-document+json@1",
        schema_version=1,
        payload_ref=f"memory:canvas:{revision}",
        fidelity=CheckpointFidelity.FULL,
    )


def _intent() -> RuntimeOperationIntent:
    checkpoint = _checkpoint()
    return RuntimeOperationIntent(
        operation_id="canvas-operation-3",
        runtime_id=checkpoint.runtime_id,
        kind=checkpoint.kind,
        alias=checkpoint.alias,
        epoch=checkpoint.epoch,
        base_revision=checkpoint.revision,
        target_revision=checkpoint.revision + 1,
        codec="canvas-operations+json@1",
        schema_version=1,
        payload='[{"op":"clear"}]',
        base_checkpoint=checkpoint,
        projections=(
            RuntimeProjectionIntent(
                intent_id="artifact",
                projector="canvas-artifact",
                schema_version=1,
            ),
        ),
    )


async def _journal(tmp_path):
    log = SessionLog("runtime-operation", base_dir=str(tmp_path))
    await log.append(SessionMetaEvent(session_id="runtime-operation"))
    return log, SessionRuntimeOperationJournal(log)


def test_session_runtime_operation_journal_satisfies_port(tmp_path):
    journal = SessionRuntimeOperationJournal(SessionLog("runtime-operation-port", base_dir=str(tmp_path)))

    assert isinstance(journal, RuntimeOperationJournal)


@pytest.mark.asyncio
async def test_prepared_operation_replays_and_supplies_base_checkpoint(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()

    await journal.prepare(intent)

    assert replay(log).pending_runtime_operations == {intent.operation_id: intent}
    recovery = await journal.recovery(
        kind="canvas",
        alias="default",
        checkpoint=None,
    )
    assert recovery.checkpoint == intent.base_checkpoint
    assert recovery.operations == (intent,)


@pytest.mark.asyncio
async def test_complete_and_abort_remove_prepared_operation(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)
    receipt = RuntimeOperationReceipt.from_intent(intent)
    await journal.complete(receipt)

    assert replay(log).pending_runtime_operations == {}
    assert replay(log).completed_runtime_operations == {intent.operation_id: receipt}

    await journal.prepare(intent)
    await journal.abort(intent.operation_id)

    assert replay(log).pending_runtime_operations == {}


@pytest.mark.asyncio
async def test_completed_operation_returns_receipt_and_rejects_payload_reuse(tmp_path):
    _log, journal = await _journal(tmp_path)
    intent = _intent()
    receipt = RuntimeOperationReceipt.from_intent(intent)
    assert await journal.prepare(intent) is None
    await journal.complete(receipt)

    assert await journal.prepare(intent) == receipt

    conflicting = RuntimeOperationIntent(
        operation_id=intent.operation_id,
        runtime_id=intent.runtime_id,
        kind=intent.kind,
        alias=intent.alias,
        epoch=intent.epoch,
        base_revision=intent.base_revision,
        target_revision=intent.target_revision,
        codec=intent.codec,
        schema_version=intent.schema_version,
        payload='[{"op":"remove","element_id":"different"}]',
        base_checkpoint=intent.base_checkpoint,
        projections=intent.projections,
    )
    with pytest.raises(Exception, match="different mutation"):
        await journal.prepare(conflicting)


@pytest.mark.asyncio
async def test_checkpoint_revision_filters_already_applied_pending_marker(tmp_path):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)

    recovery = await journal.recovery(
        kind="canvas",
        alias="default",
        checkpoint=_checkpoint(revision=intent.target_revision),
    )

    assert recovery.operations == ()
    assert replay(log).pending_runtime_operations == {}


@pytest.mark.asyncio
async def test_recovery_uses_durable_commit_checkpoint_when_marker_is_missing(
    tmp_path,
):
    log, journal = await _journal(tmp_path)
    intent = _intent()
    await journal.prepare(intent)
    await log.append(
        RuntimeCommitEvent(
            RuntimeCommitFact(
                commit_id="canvas-commit-3",
                checkpoint=_checkpoint(revision=intent.target_revision),
                projections=intent.projections,
                reason="write-commit",
            )
        )
    )

    recovery = await journal.recovery(
        kind="canvas",
        alias="default",
        checkpoint=None,
    )

    assert recovery.checkpoint == _checkpoint(revision=intent.target_revision)
    assert recovery.operations == ()
    assert replay(log).pending_runtime_operations == {}
