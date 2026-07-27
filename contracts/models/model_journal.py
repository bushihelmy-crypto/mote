"""Durable records for one logical model call and its wire attempts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mote.contracts.models.failover import (
    AttemptBudget,
    AttemptState,
    DecisionKind,
    FailoverDecision,
    FailureDisposition,
    ModelCallState,
    ModelCallSummary,
    RequestTransform,
)
from mote.contracts.models.invocation import CanonicalModelResponse, ModelUsage


class _FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCallPlannedRecord(_FrozenRecord):
    kind: Literal["call_planned"] = "call_planned"
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    routing_decision_id: str | None = None
    plan_id: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    config_revision: str = Field(min_length=1)
    endpoint_ids: tuple[str, ...] = Field(min_length=1)
    budget: AttemptBudget
    policy_id: str = Field(default="default", min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    root_started_at: datetime | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModelAttemptStartedRecord(_FrozenRecord):
    kind: Literal["attempt_started"] = "attempt_started"
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    endpoint_id: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(gt=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModelAttemptFinishedRecord(_FrozenRecord):
    kind: Literal["attempt_finished"] = "attempt_finished"
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    resume_generation: int = Field(default=0, ge=0)
    state: AttemptState
    failure: FailureDisposition | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: Decimal = Decimal("0")
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _terminal_attempt(self) -> "ModelAttemptFinishedRecord":
        if self.state not in {
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.IN_DOUBT,
        }:
            raise ValueError("attempt finished record requires a terminal state")
        if self.state is AttemptState.FAILED and self.failure is None:
            raise ValueError("failed attempt requires a failure disposition")
        if self.state is not AttemptState.FAILED and self.failure is not None:
            raise ValueError("only failed attempt may carry a failure disposition")
        return self


class ModelDecisionRecord(_FrozenRecord):
    kind: Literal["decision_applied"] = "decision_applied"
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    after_attempt_ordinal: int = Field(ge=0)
    decision: FailoverDecision
    from_endpoint_id: str = Field(min_length=1)
    to_endpoint_id: str | None = None
    transform: RequestTransform | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _consistent_decision(self) -> "ModelDecisionRecord":
        if self.decision.kind is DecisionKind.TRANSFORM_REQUEST:
            if self.transform is None:
                raise ValueError("transform decision record requires transform")
        elif self.transform is not None:
            raise ValueError("only transform decision record may carry transform")
        return self


class ModelCallFinishedRecord(_FrozenRecord):
    kind: Literal["call_finished"] = "call_finished"
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    state: ModelCallState
    selected_endpoint_id: str | None = None
    wire_attempts: int = Field(default=0, ge=0)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: Decimal = Decimal("0")
    failure: FailureDisposition | None = None
    summary: ModelCallSummary | None = None
    accepted_response: CanonicalModelResponse | None = None
    successful_attempt_id: str | None = None
    endpoint_fingerprint: str | None = None
    model_or_deployment: str | None = None
    provider: str | None = None
    transport: str | None = None
    tenant_fingerprint: str | None = None
    credential_slot_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _terminal_call(self) -> "ModelCallFinishedRecord":
        if self.state not in {
            ModelCallState.SUCCEEDED,
            ModelCallState.FAILED,
            ModelCallState.CANCELLED,
        }:
            raise ValueError("call finished record requires a terminal state")
        if self.state is ModelCallState.SUCCEEDED and self.selected_endpoint_id is None:
            raise ValueError("successful call requires selected_endpoint_id")
        if self.state is ModelCallState.FAILED and self.failure is None:
            raise ValueError("failed call requires a failure disposition")
        if self.state is not ModelCallState.FAILED and self.failure is not None:
            raise ValueError("only failed call may carry a failure disposition")
        return self


ModelCallJournalRecord = Annotated[
    Union[
        ModelCallPlannedRecord,
        ModelAttemptStartedRecord,
        ModelAttemptFinishedRecord,
        ModelDecisionRecord,
        ModelCallFinishedRecord,
    ],
    Field(discriminator="kind"),
]


class ModelCallRecovery(_FrozenRecord):
    model_call_id: str = Field(min_length=1)
    state: ModelCallState
    plan: ModelCallPlannedRecord
    original_plan: ModelCallPlannedRecord
    plans: tuple[ModelCallPlannedRecord, ...]
    attempts_started: int = Field(default=0, ge=0)
    attempts_finished: int = Field(default=0, ge=0)
    in_doubt_attempt_ids: tuple[str, ...] = ()
    attempt_starts: tuple[ModelAttemptStartedRecord, ...] = ()
    attempt_finishes: tuple[ModelAttemptFinishedRecord, ...] = ()
    decisions: tuple[ModelDecisionRecord, ...] = ()
    terminal: ModelCallFinishedRecord | None = None


__all__ = [
    "ModelAttemptFinishedRecord",
    "ModelAttemptStartedRecord",
    "ModelDecisionRecord",
    "ModelCallFinishedRecord",
    "ModelCallJournalRecord",
    "ModelCallPlannedRecord",
    "ModelCallRecovery",
]
