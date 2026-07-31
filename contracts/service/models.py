"""Provider-neutral models for externally hosted Tool capabilities."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from mote.contracts.model.failover import AttemptBudget, FailureDisposition


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceExecutionSemantics(StrEnum):
    """Whether a logical operation may be submitted again after uncertainty."""

    PURE = "pure"
    IDEMPOTENT = "idempotent"
    RECEIPT_BASED = "receipt_based"
    NON_REPEATABLE = "non_repeatable"


class ServiceAcceptance(StrEnum):
    """What is known about a failed start wire request."""

    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ServiceCallState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_DOUBT = "in_doubt"


class ServiceInvocation(_FrozenContract):
    schema_version: Literal[1] = 1
    service_call_id: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    semantics: ServiceExecutionSemantics
    idempotency_key: str = Field(min_length=1)
    governance_domain: str = "default"
    allowed_regions: frozenset[str] = frozenset()
    budget_profile: str = "default"
    trace_id: str = ""
    parent_span_id: str | None = None

    @model_validator(mode="after")
    def _idempotency_contract(self) -> "ServiceInvocation":
        if not self.idempotency_key.strip():
            raise ValueError("service invocation requires a stable idempotency key")
        return self


class ServiceResponse(_FrozenContract):
    schema_version: Literal[1] = 1
    value: JsonValue = None
    provider_request_id: str | None = None
    cost_usd: Decimal = Decimal("0")


class ServiceReceipt(_FrozenContract):
    """Secret-free durable handle for an accepted remote operation."""

    schema_version: Literal[1] = 1
    provider_operation_id: str = Field(min_length=1)
    state: dict[str, JsonValue] = Field(default_factory=dict)
    poll_after_seconds: float = Field(default=1.0, ge=0.0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _aware_expiry(self) -> "ServiceReceipt":
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("service receipt expiry must be timezone-aware")
        return self


class ServiceCompleted(_FrozenContract):
    kind: Literal["completed"] = "completed"
    response: ServiceResponse


class ServiceAccepted(_FrozenContract):
    kind: Literal["accepted"] = "accepted"
    receipt: ServiceReceipt


class ServiceFailed(_FrozenContract):
    """A definitive remote terminal failure, not a polling transport error."""

    kind: Literal["failed"] = "failed"
    failure: FailureDisposition


ServiceEndpointOutcome = Annotated[
    Union[ServiceCompleted, ServiceAccepted, ServiceFailed],
    Field(discriminator="kind"),
]


class ServiceEndpointFailure(_FrozenContract):
    disposition: FailureDisposition
    acceptance: ServiceAcceptance = ServiceAcceptance.UNKNOWN


class ServiceEndpointDescriptor(_FrozenContract):
    endpoint_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    base_url_identity: str = Field(min_length=1)
    credential_pool_id: str = Field(min_length=1)
    lifecycle_revision: str = Field(min_length=1)
    governance_domain: str = "default"
    region: str = "global"
    pricing_class: str = "default"


class ServicePlan(_FrozenContract):
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    service_call_id: str = Field(min_length=1)
    config_revision: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    endpoints: tuple[ServiceEndpointDescriptor, ...] = Field(min_length=1)
    budget: AttemptBudget = Field(default_factory=AttemptBudget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResolvedServiceResponse(_FrozenContract):
    schema_version: Literal[1] = 1
    response: ServiceResponse
    endpoint_id: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    tenant_fingerprint: str = Field(min_length=1)
    provider: str = "unknown"
    transport: str = "unknown"
    service_call_id: str = Field(min_length=1)
    successful_attempt_id: str = Field(min_length=1)


__all__ = [
    "ResolvedServiceResponse",
    "ServiceAcceptance",
    "ServiceAccepted",
    "ServiceCallState",
    "ServiceCompleted",
    "ServiceEndpointDescriptor",
    "ServiceEndpointFailure",
    "ServiceEndpointOutcome",
    "ServiceExecutionSemantics",
    "ServiceFailed",
    "ServiceInvocation",
    "ServicePlan",
    "ServiceReceipt",
    "ServiceResponse",
]
