"""Cross-backend operation ownership and external-effect guarantees."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OperationBackend(str, Enum):
    LOCAL_FILE = "local_file"
    TEMPORAL_HISTORY = "temporal_history"


class EffectCapability(str, Enum):
    NO_EXTERNAL_EFFECT = "no_external_effect"
    IDEMPOTENT_BY_KEY = "idempotent_by_key"
    RECONCILABLE_BY_RECEIPT = "reconcilable_by_receipt"
    NON_REPLAYABLE = "non_replayable"


class EffectSettlement(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class OperationOwnershipRequest:
    deployment_id: str
    operation_id: str
    holder_id: str
    backend: OperationBackend
    expected_revision: int
    effect_id: str
    effect_capability: EffectCapability

    def __post_init__(self) -> None:
        for value in (self.deployment_id, self.operation_id, self.holder_id, self.effect_id):
            if type(value) is not str or not value:
                raise ValueError("operation ownership identities must be non-empty strings")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("operation expected_revision must be non-negative")


@dataclass(frozen=True, slots=True)
class OperationOwnership:
    request: OperationOwnershipRequest
    subject: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class OperationGuarantee:
    backend: OperationBackend
    state_mutation_fenced: bool
    external_effect_replay_safe: bool
    automatic_retry_allowed: bool
    reconciliation_required: bool


def project_operation_guarantee(backend: OperationBackend, capability: EffectCapability) -> OperationGuarantee:
    return OperationGuarantee(
        backend=backend,
        state_mutation_fenced=True,
        external_effect_replay_safe=capability
        in {
            EffectCapability.NO_EXTERNAL_EFFECT,
            EffectCapability.IDEMPOTENT_BY_KEY,
        },
        automatic_retry_allowed=capability
        in {
            EffectCapability.NO_EXTERNAL_EFFECT,
            EffectCapability.IDEMPOTENT_BY_KEY,
        },
        reconciliation_required=capability
        in {
            EffectCapability.RECONCILABLE_BY_RECEIPT,
            EffectCapability.NON_REPLAYABLE,
        },
    )


__all__ = [
    "EffectCapability",
    "EffectSettlement",
    "OperationBackend",
    "OperationGuarantee",
    "OperationOwnership",
    "OperationOwnershipRequest",
    "project_operation_guarantee",
]
