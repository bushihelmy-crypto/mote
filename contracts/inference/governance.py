from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class ReservationState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    PENDING_RECONCILIATION = "pending_reconciliation"


class BudgetDimension(StrEnum):
    INFERENCE_UNIT = "inference_unit"
    TOKEN = "token"
    COST_MICRO_USD = "cost_micro_usd"
    DEPTH = "depth"
    CAPABILITY = "capability"


class BudgetScopeKind(StrEnum):
    INFERENCE = "inference"
    AGENT_ROOT = "agent_root"
    AGENT_SUBTREE = "agent_subtree"


class BudgetAdmissionDisposition(StrEnum):
    EXHAUSTED = "exhausted"
    NOT_CONFIGURED = "not_configured"
    IDENTITY_CONFLICT = "identity_conflict"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


class BudgetAdmissionError(RuntimeError):
    def __init__(self, disposition: BudgetAdmissionDisposition, message: str) -> None:
        super().__init__(message)
        self.disposition = disposition


class BudgetScope(FrozenContract):
    schema_version: Literal[1] = 1
    kind: BudgetScopeKind
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)


class BudgetReservationRequest(FrozenContract):
    schema_version: Literal[1] = 1
    reservation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    units: int = Field(ge=1)
    dimension: BudgetDimension
    scopes: tuple[BudgetScope, ...]

    @model_validator(mode="after")
    def _scopes_are_canonical(self) -> "BudgetReservationRequest":
        if not self.scopes or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("budget request scopes must be non-empty and unique")
        if self.scopes[0].tenant_id != self.tenant_id or self.scopes[0].project_id != self.project_id:
            raise ValueError("primary budget request scope identity mismatch")
        return self


class BudgetReservation(FrozenContract):
    schema_version: Literal[2] = 2
    reservation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    units: int = Field(ge=1)
    dimension: BudgetDimension = BudgetDimension.INFERENCE_UNIT
    scopes: tuple[BudgetScope, ...] = ()
    fencing_token: int = Field(ge=1)
    state: ReservationState = ReservationState.RESERVED
    expires_at: datetime

    @model_validator(mode="after")
    def _expiry_is_aware(self) -> "BudgetReservation":
        if self.expires_at.utcoffset() is None:
            raise ValueError("reservation expiry must be timezone-aware")
        scopes = self.scopes or (
            BudgetScope(
                kind=BudgetScopeKind.INFERENCE,
                tenant_id=self.tenant_id,
                project_id=self.project_id,
            ),
        )
        if len(set(scopes)) != len(scopes):
            raise ValueError("budget reservation scopes must be unique")
        if scopes[0].tenant_id != self.tenant_id or scopes[0].project_id != self.project_id:
            raise ValueError("primary budget scope must match tenant/project identity")
        object.__setattr__(self, "scopes", scopes)
        return self


class UsageSettlement(FrozenContract):
    schema_version: Literal[2] = 2
    settlement_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    actual_units: int = Field(ge=0)
    dimension: BudgetDimension = BudgetDimension.INFERENCE_UNIT
    state: ReservationState
    settled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuotaObservationKind(StrEnum):
    LIMITS = "limits"
    RETRY_AFTER = "retry_after"
    EXHAUSTED = "exhausted"
    MALFORMED = "malformed"


class ProviderQuotaObservation(FrozenContract):
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    kind: QuotaObservationKind
    remaining_requests: int | None = Field(default=None, ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    retry_after_seconds: float | None = Field(default=None, gt=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CredentialHealthVerdict(StrEnum):
    SUCCESS = "success"
    REFRESH = "refresh"
    QUARANTINE = "quarantine"
    REVOKE = "revoke"


class CredentialHealthObservation(FrozenContract):
    schema_version: Literal[1] = 1
    credential_slot_id: str = Field(min_length=1)
    credential_version: str = Field(min_length=1)
    verdict: CredentialHealthVerdict
    quarantine_seconds: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1)
