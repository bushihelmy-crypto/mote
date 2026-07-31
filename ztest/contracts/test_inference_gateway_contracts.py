from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.contracts.model.failover import (
    ExternalCommitState,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    Retryability,
)


def _digest() -> str:
    return "sha256:" + "a" * 64


def test_cross_process_deadline_uses_conservative_signal():
    sent = datetime(2026, 1, 1, tzinfo=timezone.utc)
    deadline = CrossProcessDeadline(
        deadline_utc=sent + timedelta(seconds=10),
        remaining_seconds_at_send=8,
        sent_at_utc=sent,
    )
    local = deadline.to_local_deadline(
        received_at_utc=sent + timedelta(seconds=2),
        local_monotonic=100,
        clock_skew_guard_seconds=1,
    )
    assert local == 106


def test_wire_permit_binds_taxonomy_generation_epochs_and_validity():
    now = datetime.now(timezone.utc)
    permit = WirePermit(
        attempt_id="attempt-1",
        execution_taxonomy=ExecutionTaxonomy.UNARY_FINITE_ATTEMPT,
        owner_journal_id="journal-1",
        wire_unit="http-request",
        generation_id="generation-1",
        generation_artifact_digest=_digest(),
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=4,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="session-key-1",
        audience="shared-daemon/socket-generation-1/tenant-1",
        trust_revision=1,
        backup_epoch=2,
        admission_epoch=3,
        signature="signature",
    )
    assert permit.ordinal == 1
    assert permit.backup_epoch == 2
    with pytest.raises(ValidationError, match="must follow"):
        WirePermit(**{**permit.model_dump(), "expires_at": now})


def test_attempt_event_has_one_explicit_terminal_classification():
    progress = AttemptLifecycleEvent(
        attempt_id="a",
        sequence=1,
        receipt_revision=1,
        generation_id="g",
        event_type=AttemptEventType.WIRE_STARTED,
    )
    terminal = AttemptLifecycleEvent(
        attempt_id="a",
        sequence=2,
        receipt_revision=2,
        generation_id="g",
        event_type=AttemptEventType.IN_DOUBT,
    )
    assert progress.terminal is False
    assert terminal.terminal is True


def test_unknown_external_commit_requires_an_explicit_reconcile_strategy():
    with pytest.raises(ValidationError, match="reconciliation strategy"):
        FailureDisposition(
            reason=FailureReason.TIMEOUT,
            domain=FailureDomain.TRANSPORT,
            retryability=Retryability.RECONCILE_ONLY,
            health_verdict=HealthVerdict.NEUTRAL,
            external_commit_state=ExternalCommitState.UNKNOWN,
        )
