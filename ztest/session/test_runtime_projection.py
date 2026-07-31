from __future__ import annotations

import pytest

from mote.contracts.ports.runtime.projection import RuntimeProjectionJournal
from mote.contracts.runtime import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
)
from mote.runtime.session import SessionLog, SessionMetaEvent, SessionRuntimeProjectionJournal
from mote.runtime.session.replay import replay


def _fact() -> RuntimeCommitFact:
    return RuntimeCommitFact(
        commit_id="notebook-1.1.4",
        checkpoint=RuntimeCheckpoint(
            runtime_id="notebook-1",
            kind="jupyter",
            epoch=1,
            revision=4,
            codec="notebook+json@1",
            schema_version=1,
            payload_ref="memory:notebook",
            fidelity=CheckpointFidelity.LOGICAL,
        ),
        projections=(
            RuntimeProjectionIntent(
                intent_id="ipynb",
                projector="notebook-artifact",
                schema_version=1,
            ),
        ),
        reason="write-commit",
    )


def test_session_projection_journal_satisfies_port(tmp_path):
    journal = SessionRuntimeProjectionJournal(SessionLog("runtime-projection-port", base_dir=str(tmp_path)))

    assert isinstance(journal, RuntimeProjectionJournal)


@pytest.mark.asyncio
async def test_session_projection_journal_replays_pending_then_acknowledged(tmp_path):
    log = SessionLog("runtime-projection", base_dir=str(tmp_path))
    await log.append(SessionMetaEvent(session_id="runtime-projection"))
    journal = SessionRuntimeProjectionJournal(log)
    fact = _fact()

    await journal.record_commit(fact)
    pending = replay(log).pending_runtime_projections
    assert list(pending.values())[0].intent == fact.projections[0]

    await journal.acknowledge(
        RuntimeProjectionAck(
            commit_id=fact.commit_id,
            intent_id=fact.projections[0].intent_id,
        )
    )

    assert replay(log).pending_runtime_projections == {}


@pytest.mark.asyncio
async def test_session_projection_journal_propagates_durable_write_failure(tmp_path, monkeypatch):
    log = SessionLog("runtime-projection-failure", base_dir=str(tmp_path))
    await log.append(SessionMetaEvent(session_id="runtime-projection-failure"))
    journal = SessionRuntimeProjectionJournal(log)

    def fail_append(descriptor):
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "mote.runtime.events.journal.os.fsync",
        fail_append,
    )

    with pytest.raises(OSError, match="disk unavailable"):
        await journal.record_commit(_fact())
