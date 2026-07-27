"""Stable provider-neutral contracts for one logical model call."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from mote.contracts.models.invocation import RequestRequirements


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FailureReason(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    AUTH_REJECTED = "auth_rejected"
    BILLING_EXHAUSTED = "billing_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTEXT_EXCEEDED = "context_exceeded"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    IMAGE_TOO_LARGE = "image_too_large"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    RESPONSE_EMPTY = "response_empty"
    RESPONSE_UNPARSABLE = "response_unparsable"
    CONTENT_POLICY = "content_policy"
    UNKNOWN = "unknown"


class FailureDomain(StrEnum):
    TRANSPORT = "transport"
    ENDPOINT = "endpoint"
    CREDENTIAL = "credential"
    REQUEST = "request"
    RESPONSE = "response"
    GOVERNANCE = "governance"
    UNKNOWN = "unknown"


class Retryability(StrEnum):
    SAME_ENDPOINT = "same_endpoint"
    AFTER_CHANGE = "after_change"
    NEVER = "never"


class HealthVerdict(StrEnum):
    AVAILABILITY_FAILURE = "availability_failure"
    QUOTA_LIMITED = "quota_limited"
    CREDENTIAL_REJECTED = "credential_rejected"
    NEUTRAL = "neutral"


class DecisionKind(StrEnum):
    RETRY_SAME_ENDPOINT = "retry_same_endpoint"
    ROTATE_CREDENTIAL = "rotate_credential"
    TRANSFORM_REQUEST = "transform_request"
    SWITCH_ENDPOINT = "switch_endpoint"
    ABORT = "abort"


class RequestTransform(StrEnum):
    COMPRESS = "compress"
    SHRINK_IMAGE = "shrink_image"
    DOWNGRADE_TOOL_CONTENT = "downgrade_tool_content"
    STRIP_REQUEST_STATE = "strip_request_state"


class AttemptState(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_DOUBT = "in_doubt"


class ModelCallState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_DOUBT = "in_doubt"


class AdmissionGate(StrEnum):
    OPERATOR = "operator"
    DEADLINE = "deadline"
    CREDENTIAL = "credential"
    QUOTA = "quota"
    AVAILABILITY = "availability"
    BULKHEAD = "bulkhead"


class OperatorState(StrEnum):
    ENABLED = "enabled"
    DRAINING = "draining"
    DISABLED = "disabled"


class FailureDisposition(_FrozenContract):
    schema_version: Literal[1] = 1
    reason: FailureReason
    domain: FailureDomain
    retryability: Retryability
    health_verdict: HealthVerdict = HealthVerdict.NEUTRAL
    retry_after_seconds: float | None = Field(default=None, gt=0)
    status_code: int | None = None
    provider_code: str | None = None
    safe_detail: dict[str, JsonValue] = Field(default_factory=dict)


class FailoverDecision(_FrozenContract):
    schema_version: Literal[1] = 1
    kind: DecisionKind
    reason: str = Field(min_length=1)
    target_endpoint_id: str | None = None
    transform: RequestTransform | None = None
    delay_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _validate_transform(self) -> "FailoverDecision":
        if self.kind is DecisionKind.TRANSFORM_REQUEST and self.transform is None:
            raise ValueError("transform_request decision requires transform")
        if self.kind is not DecisionKind.TRANSFORM_REQUEST and self.transform is not None:
            raise ValueError("only transform_request decision may carry transform")
        return self


class AttemptBudget(_FrozenContract):
    max_wire_attempts: int = Field(default=6, ge=1)
    max_attempts_per_endpoint: int = Field(default=6, ge=1)
    max_endpoint_switches: int = Field(default=5, ge=0)
    max_credential_rotations: int = Field(default=5, ge=0)
    max_request_transforms: int = Field(default=5, ge=0)
    total_deadline_seconds: float = Field(default=600.0, gt=0.0)
    single_attempt_timeout_seconds: float = Field(default=180.0, gt=0.0)
    max_backoff_seconds: float = Field(default=60.0, ge=0.0)

    @model_validator(mode="after")
    def _bounded_by_wire_budget(self) -> "AttemptBudget":
        if self.max_attempts_per_endpoint > self.max_wire_attempts:
            raise ValueError("max_attempts_per_endpoint cannot exceed max_wire_attempts")
        max_changes = self.max_wire_attempts - 1
        for name in (
            "max_endpoint_switches",
            "max_credential_rotations",
            "max_request_transforms",
        ):
            if getattr(self, name) > max_changes:
                raise ValueError(f"{name} cannot exceed max_wire_attempts - 1")
        if self.single_attempt_timeout_seconds > self.total_deadline_seconds:
            raise ValueError("single_attempt_timeout_seconds cannot exceed total_deadline_seconds")
        return self


class EndpointCapabilities(_FrozenContract):
    supports_tools: bool = False
    supports_native_schema: bool = False
    supports_server_web_search: bool = False
    supports_vision: bool = False
    supports_pdf: bool = False
    supports_native_tool_search: bool = False
    context_tokens: int = Field(default=0, ge=0)


class EndpointDescriptor(_FrozenContract):
    endpoint_id: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url_identity: str = Field(min_length=1)
    capabilities: EndpointCapabilities = Field(default_factory=EndpointCapabilities)
    governance_domain: str = "default"
    region: str = "global"
    pricing_class: str = "default"
    credential_pool_id: str = Field(min_length=1)
    lifecycle_revision: str = Field(min_length=1)


class FailoverPlan(_FrozenContract):
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    model_call_id: str = Field(min_length=1)
    config_revision: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    endpoints: tuple[EndpointDescriptor, ...] = Field(min_length=1)
    requirements: RequestRequirements = Field(default_factory=RequestRequirements)
    budget: AttemptBudget = Field(default_factory=AttemptBudget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResourceIdentity(_FrozenContract):
    endpoint_id: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    model_or_deployment: str = Field(min_length=1)
    tenant_fingerprint: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)


class OperatorTransition(_FrozenContract):
    """One durably audited endpoint control-state transition."""

    schema_version: Literal[1] = 1
    resource: ResourceIdentity
    previous_state: OperatorState
    state: OperatorState
    control_revision: int = Field(ge=1)
    config_revision: str = Field(min_length=1, max_length=256)
    actor: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2048)
    force: bool = False
    in_flight: int = Field(default=0, ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_transition(self) -> "OperatorTransition":
        if self.previous_state is self.state:
            raise ValueError("operator transition must change state")
        if self.occurred_at.utcoffset() is None:
            raise ValueError("operator transition time must be timezone-aware")
        return self


class OperatorStatus(_FrozenContract):
    resource: ResourceIdentity
    state: OperatorState
    control_revision: int = Field(default=0, ge=0)
    in_flight: int = Field(default=0, ge=0)
    drained: bool = False


class AdmissionVerdict(_FrozenContract):
    gate: AdmissionGate
    reason: str = Field(min_length=1)
    resource: ResourceIdentity
    disposition: FailureDisposition


class AttemptSummary(_FrozenContract):
    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    endpoint_id: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    state: AttemptState
    failure: FailureDisposition | None = None
    latency_seconds: float = Field(default=0.0, ge=0.0)
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: Decimal = Decimal("0")


class ModelCallSummary(_FrozenContract):
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    routing_decision_id: str | None = None
    plan_id: str = Field(min_length=1)
    config_revision: str = Field(min_length=1)
    policy_id: str = Field(default="default", min_length=1)
    resume_generation: int = Field(default=0, ge=0)
    state: ModelCallState
    attempts: tuple[AttemptSummary, ...] = ()
    wire_attempts_used: int = Field(default=0, ge=0)
    endpoint_switches: int = Field(default=0, ge=0)
    credential_rotations: int = Field(default=0, ge=0)
    request_transforms: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    selected_endpoint_id: str | None = None
    known_usage: dict[str, int] = Field(default_factory=dict)
    known_cost_usd: Decimal = Decimal("0")
    in_doubt_attempt_ids: tuple[str, ...] = ()
    possible_duplicate_billing: bool = False
    last_failure: FailureDisposition | None = None


__all__ = [
    "AdmissionGate",
    "AdmissionVerdict",
    "AttemptBudget",
    "AttemptState",
    "AttemptSummary",
    "DecisionKind",
    "EndpointCapabilities",
    "EndpointDescriptor",
    "FailoverDecision",
    "FailoverPlan",
    "FailureDisposition",
    "FailureDomain",
    "FailureReason",
    "HealthVerdict",
    "ModelCallState",
    "ModelCallSummary",
    "OperatorState",
    "OperatorStatus",
    "OperatorTransition",
    "RequestTransform",
    "ResourceIdentity",
    "Retryability",
]
