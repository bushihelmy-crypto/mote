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


class BudgetReservation(FrozenContract):
    schema_version: Literal[1] = 1
    reservation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    units: int = Field(ge=1)
    fencing_token: int = Field(ge=1)
    state: ReservationState = ReservationState.RESERVED
    expires_at: datetime

    @model_validator(mode="after")
    def _expiry_is_aware(self) -> "BudgetReservation":
        if self.expires_at.utcoffset() is None:
            raise ValueError("reservation expiry must be timezone-aware")
        return self


class UsageSettlement(FrozenContract):
    schema_version: Literal[1] = 1
    settlement_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    actual_units: int = Field(ge=0)
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
