from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mote.contracts.inference.execution_owner import (
    ExecutionEpochBinding,
    ExecutionId,
    ExecutionObjectCommand,
    ExecutionOwnerDisposition,
    ExecutionOwnerRecord,
    ExecutionOwnerVerification,
    SharedExecutionVariant,
    epoch_binding_from_permit,
    verify_execution_owner,
    verify_execution_permit_binding,
)
from mote.contracts.inference.identity import InferencePrincipal
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64


def _principal(subject: str = "subject") -> InferencePrincipal:
    return InferencePrincipal(
        tenant_id="tenant",
        project_id="project",
        subject_id=subject,
        policy_revision="policy-1",
        delegation_digest="sha256:" + "b" * 64,
    )


def _epoch(admission: int = 4) -> ExecutionEpochBinding:
    return ExecutionEpochBinding(backup_epoch=2, admission_epoch=admission, permit_trust_revision=3)


def _record(variant: SharedExecutionVariant = SharedExecutionVariant.FINITE) -> ExecutionOwnerRecord:
    return ExecutionOwnerRecord(
        record_revision=7,
        execution_id=ExecutionId("execution-1"),
        variant=variant,
        principal=_principal(),
        application_scope="application-1",
        credential_scope="credential-session-1",
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        epoch=_epoch(),
    )


def _request(**changes: object) -> ExecutionOwnerVerification:
    values: dict[str, object] = {
        "execution_id": ExecutionId("execution-1"),
        "command": ExecutionObjectCommand.AUTHORIZE,
        "principal": _principal(),
        "application_scope": "application-1",
        "credential_scope": "credential-session-1",
        "generation_id": "generation-1",
        "generation_artifact_digest": DIGEST,
        "epoch": _epoch(),
    }
    values.update(changes)
    return ExecutionOwnerVerification.model_validate(values)


def test_owner_record_has_canonical_strict_round_trip() -> None:
    record = _record()
    assert ExecutionOwnerRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        ExecutionOwnerRecord.model_validate({**record.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        ExecutionOwnerRecord.model_validate({**record.model_dump(), "schema_version": 2})
    with pytest.raises(ValidationError):
        ExecutionOwnerRecord.model_validate({**record.model_dump(), "variant": "future"})


@pytest.mark.parametrize(
    ("change", "disposition"),
    [
        ({"execution_id": ExecutionId("other")}, ExecutionOwnerDisposition.DENIED_EXECUTION_ID),
        ({"principal": _principal("other")}, ExecutionOwnerDisposition.DENIED_PRINCIPAL),
        ({"application_scope": "other"}, ExecutionOwnerDisposition.DENIED_APPLICATION_SCOPE),
        ({"credential_scope": "other"}, ExecutionOwnerDisposition.DENIED_CREDENTIAL_SCOPE),
        ({"generation_id": "other"}, ExecutionOwnerDisposition.DENIED_GENERATION),
        ({"generation_artifact_digest": "sha256:" + "c" * 64}, ExecutionOwnerDisposition.DENIED_ARTIFACT),
        ({"epoch": _epoch(5)}, ExecutionOwnerDisposition.DENIED_EPOCH),
    ],
)
def test_each_owner_binding_mismatch_is_a_typed_denial(
    change: dict[str, object], disposition: ExecutionOwnerDisposition
) -> None:
    assert verify_execution_owner(_record(), _request(**change)).disposition is disposition


def test_finite_execution_rejects_session_only_command() -> None:
    request = _request(command=ExecutionObjectCommand.SEND_SESSION_MESSAGE)
    assert (
        verify_execution_owner(_record(SharedExecutionVariant.FINITE), request).disposition
        is ExecutionOwnerDisposition.DENIED_COMMAND_FOR_VARIANT
    )
    assert verify_execution_owner(_record(SharedExecutionVariant.SESSION), request).allowed


def test_wire_permit_epoch_uses_the_same_binding_contract() -> None:
    now = datetime.now(UTC)
    permit = WirePermit(
        attempt_id="execution-1",
        execution_taxonomy=ExecutionTaxonomy.UNARY_FINITE_ATTEMPT,
        owner_journal_id="journal-1",
        wire_unit="unit-1",
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        ordinal=1,
        nonce="n" * 16,
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key-1",
        audience="daemon",
        trust_revision=3,
        backup_epoch=2,
        admission_epoch=4,
        signature="signature",
    )
    assert epoch_binding_from_permit(permit) == _epoch()
    assert verify_execution_permit_binding(_record(), permit).allowed
    assert (
        verify_execution_permit_binding(_record(), permit.model_copy(update={"attempt_id": "other"})).disposition
        is ExecutionOwnerDisposition.DENIED_PERMIT_EXECUTION
    )
    assert (
        verify_execution_permit_binding(
            _record(),
            permit.model_copy(update={"execution_taxonomy": ExecutionTaxonomy.LONG_LIVED_SESSION}),
        ).disposition
        is ExecutionOwnerDisposition.DENIED_PERMIT_VARIANT
    )


def test_contract_has_no_product_or_protobuf_dependency() -> None:
    source = (ROOT / "contracts/inference/execution_owner.py").read_text(encoding="utf-8")
    assert "product" not in source
    assert "protobuf" not in source
    assert "grpc" not in source
    assert '"LEGAL_EXECUTION_COMMANDS",' not in source
    assert "_LEGAL_EXECUTION_COMMANDS = MappingProxyType(" in source
