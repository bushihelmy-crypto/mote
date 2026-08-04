from __future__ import annotations

from pathlib import Path

import pytest

from mote.contracts.model import (
    AttemptBudget,
    AttemptState,
    ModelAttemptFinishedRecord,
    ModelAttemptStartedRecord,
    ModelCallFinishedRecord,
    ModelCallPlannedRecord,
    ModelCallState,
    ModelWireAuthorizedRecord,
)
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.runtime.models.failover import LocalModelCallJournal as _LocalModelCallJournal
from mote.runtime.models.failover import ModelCallJournalIntegrityError


def LocalModelCallJournal(root):
    return _LocalModelCallJournal(root, policy=approved_model_checkpoint_policy())


def _planned(call_id: str) -> ModelCallPlannedRecord:
    return ModelCallPlannedRecord(
        model_call_id=call_id,
        plan_id=f"plan:{call_id}",
        route_id="default",
        runtime_generation_id="runtime-test",
        topology_revision="topology-test",
        config_revision="revision-1",
        endpoint_ids=("primary",),
        budget=AttemptBudget(),
    )


@pytest.mark.parametrize("route_id", ["strong", "unknown:strong", "task:"])
def test_planned_record_rejects_noncanonical_route_id(route_id: str) -> None:
    with pytest.raises(ValueError, match="route id"):
        payload = _planned("call-legacy").model_dump()
        payload["route_id"] = route_id
        ModelCallPlannedRecord.model_validate(payload)


def _started(call_id: str, ordinal: int = 1) -> ModelAttemptStartedRecord:
    return ModelAttemptStartedRecord(
        model_call_id=call_id,
        attempt_id=f"{call_id}:{ordinal}",
        ordinal=ordinal,
        endpoint_id="primary",
        endpoint_fingerprint="endpoint-fingerprint",
        credential_slot_id="primary:0",
        timeout_seconds=10,
    )


def _finished(
    call_id: str,
    ordinal: int = 1,
    state: AttemptState = AttemptState.SUCCEEDED,
) -> ModelAttemptFinishedRecord:
    return ModelAttemptFinishedRecord(
        model_call_id=call_id,
        attempt_id=f"{call_id}:{ordinal}",
        ordinal=ordinal,
        state=state,
    )


def _authorized(call_id: str, ordinal: int = 1) -> ModelWireAuthorizedRecord:
    return ModelWireAuthorizedRecord(
        model_call_id=call_id,
        attempt_id=f"{call_id}:{ordinal}",
        ordinal=ordinal,
        issued_journal_revision=3,
        permit_digest="sha256:" + "a" * 64,
    )


def test_wire_authorization_is_single_and_matches_open_attempt(tmp_path):
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call"))
    journal.append_committed(_started("call"))
    journal.append_committed(_authorized("call"))
    with pytest.raises(ModelCallJournalIntegrityError, match="authorization"):
        journal.append_committed(_authorized("call"))


def test_journal_requires_plan_as_first_record(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)

    with pytest.raises(ModelCallJournalIntegrityError, match="must begin"):
        journal.append_committed(_started("call-1"))


def test_journal_rejects_non_contiguous_attempts_and_duplicate_terminal(
    tmp_path: Path,
) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-1"))

    with pytest.raises(ModelCallJournalIntegrityError, match="ordinal"):
        journal.append_committed(_started("call-1", ordinal=2))

    journal.append_committed(_started("call-1"))
    journal.append_committed(_finished("call-1"))
    with pytest.raises(ModelCallJournalIntegrityError, match="terminal"):
        journal.append_committed(_finished("call-1"))


def test_journal_rejects_call_terminal_with_open_attempt(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-1"))
    journal.append_committed(_started("call-1"))

    with pytest.raises(ModelCallJournalIntegrityError, match="open attempt"):
        journal.append_committed(
            ModelCallFinishedRecord(
                model_call_id="call-1",
                state=ModelCallState.CANCELLED,
                wire_attempts=1,
            )
        )


def test_complete_success_recovers_as_succeeded(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-1"))
    journal.append_committed(_started("call-1"))
    journal.append_committed(_finished("call-1"))
    terminal = ModelCallFinishedRecord(
        model_call_id="call-1",
        state=ModelCallState.SUCCEEDED,
        selected_endpoint_id="primary",
        wire_attempts=1,
    )
    journal.append_committed(terminal)

    recovery = journal.recover("call-1")

    assert recovery.state is ModelCallState.SUCCEEDED
    assert recovery.attempts_started == 1
    assert recovery.attempts_finished == 1
    assert recovery.in_doubt_attempt_ids == ()
    assert recovery.terminal == terminal


def test_unfinished_started_attempt_recovers_as_in_doubt(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-1"))
    journal.append_committed(_started("call-1"))

    recovery = journal.recover("call-1")

    assert recovery.state is ModelCallState.IN_DOUBT
    assert recovery.in_doubt_attempt_ids == ("call-1:1",)


def test_in_doubt_scans_calls_without_mixing_streams(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("uncertain"))
    journal.append_committed(_started("uncertain"))
    journal.append_committed(_planned("complete"))
    journal.append_committed(
        ModelCallFinishedRecord(
            model_call_id="complete",
            state=ModelCallState.CANCELLED,
        )
    )

    assert [recovery.model_call_id for recovery in journal.in_doubt()] == ["uncertain"]


def test_truncated_record_fails_closed(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-1"))
    with journal.path_for("call-1").open("ab") as stream:
        stream.write(b'{"kind":"attempt_started"')

    with pytest.raises(ModelCallJournalIntegrityError, match="incomplete"):
        journal.recover("call-1")


def test_call_id_is_hashed_and_cannot_escape_root(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    path = journal.path_for("../../outside/secret")

    assert path.parent == tmp_path
    assert path.name.endswith(".jsonl")
    assert "outside" not in path.name


def test_concurrent_call_ids_use_distinct_files(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    journal.append_committed(_planned("call-a"))
    journal.append_committed(_planned("call-b"))

    assert journal.path_for("call-a") != journal.path_for("call-b")
    assert [record.model_call_id for record in journal.records("call-a")] == ["call-a"]
    assert [record.model_call_id for record in journal.records("call-b")] == ["call-b"]
