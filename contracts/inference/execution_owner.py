"""Canonical Shared execution ownership and object-authorization facts."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, NewType, Protocol

from pydantic import Field

from mote.contracts.inference.base import FrozenContract
from mote.contracts.inference.identity import InferencePrincipal
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit

ExecutionId = NewType("ExecutionId", str)
BoundExecutionId = Annotated[ExecutionId, Field(min_length=1, max_length=256)]


class SharedExecutionVariant(StrEnum):
    FINITE = "finite"
    SESSION = "session"


class ExecutionObjectCommand(StrEnum):
    AUTHORIZE = "authorize"
    CANCEL = "cancel"
    STREAM_EVENTS = "stream_events"
    RECONCILE = "reconcile"
    QUERY_RECEIPT = "query_receipt"
    SEND_SESSION_MESSAGE = "send_session_message"


_LEGAL_EXECUTION_COMMANDS = MappingProxyType(
    {
        SharedExecutionVariant.FINITE: frozenset(
            {
                ExecutionObjectCommand.AUTHORIZE,
                ExecutionObjectCommand.CANCEL,
                ExecutionObjectCommand.STREAM_EVENTS,
                ExecutionObjectCommand.RECONCILE,
                ExecutionObjectCommand.QUERY_RECEIPT,
            }
        ),
        SharedExecutionVariant.SESSION: frozenset(ExecutionObjectCommand),
    }
)


class ExecutionEpochBinding(FrozenContract):
    schema_version: Literal[1] = 1
    backup_epoch: int = Field(ge=0)
    admission_epoch: int = Field(ge=0)
    permit_trust_revision: int = Field(ge=1)


class ExecutionOwnerRecord(FrozenContract):
    schema_version: Literal[1] = 1
    record_revision: int = Field(ge=1)
    execution_id: BoundExecutionId
    variant: SharedExecutionVariant
    principal: InferencePrincipal
    application_scope: str = Field(min_length=1, max_length=256)
    credential_scope: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=256)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    epoch: ExecutionEpochBinding


class ExecutionOwnerVerification(FrozenContract):
    schema_version: Literal[1] = 1
    execution_id: BoundExecutionId
    command: ExecutionObjectCommand
    principal: InferencePrincipal
    application_scope: str = Field(min_length=1, max_length=256)
    credential_scope: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=256)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    epoch: ExecutionEpochBinding


class ExecutionOwnerDisposition(StrEnum):
    ALLOWED = "allowed"
    DENIED_EXECUTION_ID = "denied_execution_id"
    DENIED_COMMAND_FOR_VARIANT = "denied_command_for_variant"
    DENIED_PRINCIPAL = "denied_principal"
    DENIED_APPLICATION_SCOPE = "denied_application_scope"
    DENIED_CREDENTIAL_SCOPE = "denied_credential_scope"
    DENIED_GENERATION = "denied_generation"
    DENIED_ARTIFACT = "denied_artifact"
    DENIED_EPOCH = "denied_epoch"
    DENIED_PERMIT_EXECUTION = "denied_permit_execution"
    DENIED_PERMIT_VARIANT = "denied_permit_variant"


class ExecutionOwnerDecision(FrozenContract):
    schema_version: Literal[1] = 1
    disposition: ExecutionOwnerDisposition
    record_revision: int = Field(ge=1)

    @property
    def allowed(self) -> bool:
        return self.disposition is ExecutionOwnerDisposition.ALLOWED


class ExecutionOwnerVerifier(Protocol):
    def verify(
        self,
        record: ExecutionOwnerRecord,
        request: ExecutionOwnerVerification,
    ) -> ExecutionOwnerDecision: ...


def verify_execution_owner(
    record: ExecutionOwnerRecord,
    request: ExecutionOwnerVerification,
) -> ExecutionOwnerDecision:
    """Compare every immutable object binding in deterministic denial order."""

    disposition = ExecutionOwnerDisposition.ALLOWED
    if request.execution_id != record.execution_id:
        disposition = ExecutionOwnerDisposition.DENIED_EXECUTION_ID
    elif request.command not in _LEGAL_EXECUTION_COMMANDS[record.variant]:
        disposition = ExecutionOwnerDisposition.DENIED_COMMAND_FOR_VARIANT
    elif request.principal != record.principal:
        disposition = ExecutionOwnerDisposition.DENIED_PRINCIPAL
    elif request.application_scope != record.application_scope:
        disposition = ExecutionOwnerDisposition.DENIED_APPLICATION_SCOPE
    elif request.credential_scope != record.credential_scope:
        disposition = ExecutionOwnerDisposition.DENIED_CREDENTIAL_SCOPE
    elif request.generation_id != record.generation_id:
        disposition = ExecutionOwnerDisposition.DENIED_GENERATION
    elif request.generation_artifact_digest != record.generation_artifact_digest:
        disposition = ExecutionOwnerDisposition.DENIED_ARTIFACT
    elif request.epoch != record.epoch:
        disposition = ExecutionOwnerDisposition.DENIED_EPOCH
    return ExecutionOwnerDecision(disposition=disposition, record_revision=record.record_revision)


def execution_variant_for_taxonomy(taxonomy: ExecutionTaxonomy) -> SharedExecutionVariant:
    if taxonomy is ExecutionTaxonomy.LONG_LIVED_SESSION:
        return SharedExecutionVariant.SESSION
    return SharedExecutionVariant.FINITE


def epoch_binding_from_permit(permit: WirePermit) -> ExecutionEpochBinding:
    return ExecutionEpochBinding(
        backup_epoch=permit.backup_epoch,
        admission_epoch=permit.admission_epoch,
        permit_trust_revision=permit.trust_revision,
    )


def verify_execution_permit_binding(
    record: ExecutionOwnerRecord,
    permit: WirePermit,
) -> ExecutionOwnerDecision:
    disposition = ExecutionOwnerDisposition.ALLOWED
    if permit.attempt_id != record.execution_id:
        disposition = ExecutionOwnerDisposition.DENIED_PERMIT_EXECUTION
    elif execution_variant_for_taxonomy(permit.execution_taxonomy) is not record.variant:
        disposition = ExecutionOwnerDisposition.DENIED_PERMIT_VARIANT
    elif permit.generation_id != record.generation_id:
        disposition = ExecutionOwnerDisposition.DENIED_GENERATION
    elif permit.generation_artifact_digest != record.generation_artifact_digest:
        disposition = ExecutionOwnerDisposition.DENIED_ARTIFACT
    elif epoch_binding_from_permit(permit) != record.epoch:
        disposition = ExecutionOwnerDisposition.DENIED_EPOCH
    return ExecutionOwnerDecision(disposition=disposition, record_revision=record.record_revision)


__all__ = [
    "BoundExecutionId",
    "ExecutionEpochBinding",
    "ExecutionId",
    "ExecutionObjectCommand",
    "ExecutionOwnerDecision",
    "ExecutionOwnerDisposition",
    "ExecutionOwnerRecord",
    "ExecutionOwnerVerification",
    "ExecutionOwnerVerifier",
    "SharedExecutionVariant",
    "epoch_binding_from_permit",
    "execution_variant_for_taxonomy",
    "verify_execution_owner",
    "verify_execution_permit_binding",
]
