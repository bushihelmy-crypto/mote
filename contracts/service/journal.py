"""Durable records for one externally hosted Tool service call."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mote.contracts.model.failover import AttemptBudget, AttemptState, FailoverDecision, FailureDisposition
from mote.contracts.service.models import (
    HostedServicePayload,
    ServiceCallState,
    ServiceExecutionSemantics,
    ServiceReceipt,
    ServiceResponse,
)


class _FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceCallPlannedRecord(_FrozenRecord):
    kind: Literal["service_call_planned"] = "service_call_planned"
    schema_version: Literal[2] = 2
    service_call_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    payload: HostedServicePayload
    config_revision: str = Field(min_length=1)
    endpoint_ids: tuple[str, ...] = Field(min_length=1)
    budget: AttemptBudget
    policy_id: str = Field(min_length=1)
    semantics: ServiceExecutionSemantics
    idempotency_key: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    root_started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceAttemptStartedRecord(_FrozenRecord):
    kind: Literal["service_attempt_started"] = "service_attempt_started"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    endpoint_id: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(gt=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceReceiptAcceptedRecord(_FrozenRecord):
    kind: Literal["service_receipt_accepted"] = "service_receipt_accepted"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    receipt: ServiceReceipt
    poll_ordinal: int = Field(default=0, ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceCallSuspendedRecord(_FrozenRecord):
    kind: Literal["service_call_suspended"] = "service_call_suspended"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    reason: Literal["deadline"] = "deadline"
    resume_generation: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceAttemptFinishedRecord(_FrozenRecord):
    kind: Literal["service_attempt_finished"] = "service_attempt_finished"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    resume_generation: int = Field(default=0, ge=0)
    state: AttemptState
    failure: FailureDisposition | None = None
    response: ServiceResponse | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_outcome(self) -> "ServiceAttemptFinishedRecord":
        if self.state in {AttemptState.PLANNED, AttemptState.STARTED}:
            raise ValueError("service attempt finish requires a terminal state")
        if self.state is AttemptState.SUCCEEDED:
            if self.response is None or self.failure is not None:
                raise ValueError("succeeded service attempt requires only a response")
        elif self.state in {AttemptState.FAILED, AttemptState.IN_DOUBT}:
            if self.failure is None or self.response is not None:
                raise ValueError("failed service attempt requires only a failure")
        elif self.response is not None or self.failure is not None:
            raise ValueError("cancelled service attempt cannot carry an outcome")
        return self


class ServiceDecisionRecord(_FrozenRecord):
    kind: Literal["service_decision_applied"] = "service_decision_applied"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    after_attempt_ordinal: int = Field(ge=0)
    decision: FailoverDecision
    from_endpoint_id: str = Field(min_length=1)
    to_endpoint_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceCallFinishedRecord(_FrozenRecord):
    kind: Literal["service_call_finished"] = "service_call_finished"
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    state: ServiceCallState
    selected_endpoint_id: str | None = None
    successful_attempt_id: str | None = None
    endpoint_fingerprint: str | None = None
    credential_slot_id: str | None = None
    tenant_fingerprint: str | None = None
    provider: str | None = None
    transport: str | None = None
    response: ServiceResponse | None = None
    failure: FailureDisposition | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_outcome(self) -> "ServiceCallFinishedRecord":
        if self.state in {
            ServiceCallState.PLANNED,
            ServiceCallState.RUNNING,
            ServiceCallState.WAITING_REMOTE,
        }:
            raise ValueError("service call finish requires a terminal state")
        success_fields = (
            self.selected_endpoint_id,
            self.successful_attempt_id,
            self.endpoint_fingerprint,
            self.credential_slot_id,
            self.tenant_fingerprint,
            self.response,
        )
        if self.state is ServiceCallState.SUCCEEDED:
            if any(value is None for value in success_fields) or self.failure is not None:
                raise ValueError("succeeded service call requires complete response provenance")
        elif self.state in {ServiceCallState.FAILED, ServiceCallState.IN_DOUBT}:
            if self.failure is None or self.response is not None:
                raise ValueError("failed service call requires only a failure")
        elif self.response is not None or self.failure is not None:
            raise ValueError("cancelled service call cannot carry an outcome")
        return self


ServiceCallJournalRecord = Annotated[
    Union[
        ServiceCallPlannedRecord,
        ServiceAttemptStartedRecord,
        ServiceReceiptAcceptedRecord,
        ServiceCallSuspendedRecord,
        ServiceAttemptFinishedRecord,
        ServiceDecisionRecord,
        ServiceCallFinishedRecord,
    ],
    Field(discriminator="kind"),
]


class ServiceCallRecovery(_FrozenRecord):
    service_call_id: str = Field(min_length=1)
    state: ServiceCallState
    plan: ServiceCallPlannedRecord
    plans: tuple[ServiceCallPlannedRecord, ...]
    attempt_starts: tuple[ServiceAttemptStartedRecord, ...] = ()
    receipts: tuple[ServiceReceiptAcceptedRecord, ...] = ()
    suspensions: tuple[ServiceCallSuspendedRecord, ...] = ()
    attempt_finishes: tuple[ServiceAttemptFinishedRecord, ...] = ()
    decisions: tuple[ServiceDecisionRecord, ...] = ()
    terminal: ServiceCallFinishedRecord | None = None


__all__ = [
    "ServiceAttemptFinishedRecord",
    "ServiceAttemptStartedRecord",
    "ServiceCallFinishedRecord",
    "ServiceCallJournalRecord",
    "ServiceCallPlannedRecord",
    "ServiceCallRecovery",
    "ServiceCallSuspendedRecord",
    "ServiceDecisionRecord",
    "ServiceReceiptAcceptedRecord",
]
