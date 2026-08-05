"""Fenced transaction contracts for a single agent run frontier."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Literal


@dataclass(frozen=True, slots=True)
class ExecutionOperationContext:
    run_id: str
    attempt_id: str
    operation_id: str
    fencing_token: int
    expected_revision: int | None = None


class MutationStatus(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    CONFLICT = "conflict"
    FENCED = "fenced"
    CANCELLED = "cancelled"


class InferenceCheckpointAttemptState(str, Enum):
    INTENT_COMMITTED = "intent_committed"
    WIRE_STARTED = "wire_started"
    SETTLED = "settled"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class MutationResult:
    status: MutationStatus
    revision: int | None = None
    reference_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class InferenceCompleted:
    pass


@dataclass(frozen=True, slots=True)
class InferenceStopped:
    pass


InferenceDisposition = InferenceCompleted | InferenceStopped


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryFrontier:
    revision: int
    model_call_id: str = ""
    model_call_state: str = "not_started"
    target_id: str = ""
    target_lease_id: str = ""
    attempt_id: str = ""
    capability_fingerprint: str = ""
    projection_compatibility_key: str = ""
    tool_snapshot_id: str = ""
    terminal_committed: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class InferenceCheckpointState:
    model_call_id: str
    schema_version: Literal[1] = 1
    attempt_state: InferenceCheckpointAttemptState = InferenceCheckpointAttemptState.INTENT_COMMITTED
    target_id: str = ""
    route_schema_version: int = 2
    target_lease_id: str = ""
    target_lease_expires_at: float = 0.0
    inference_attempt_id: str = ""
    inference_fencing_token: int = 0
    capability_fingerprint: str = ""
    projection_compatibility_key: str = ""
    tool_snapshot_id: str = ""
    tool_registry_revision: int = 0
    protocol_fingerprint: str = ""
    vocabulary_fingerprint: str = ""
    tool_projection_fingerprint: str = ""
    prompt_section_set_fingerprint: str = ""
    request_fingerprint: str = ""

    def __post_init__(self) -> None:
        if type(self.model_call_id) is not str or not self.model_call_id:
            raise ValueError("inference checkpoint requires a ModelCall identity")
        if self.schema_version != 1:
            raise ValueError("unsupported inference checkpoint schema")
        if not isinstance(self.attempt_state, InferenceCheckpointAttemptState):
            raise ValueError("invalid inference checkpoint attempt state")
        target_values = (self.target_id, self.target_lease_id)
        if any(target_values) != all(target_values):
            raise ValueError("inference target identity must be complete or absent")
        if self.target_lease_expires_at and (
            not isinstance(self.target_lease_expires_at, (int, float))
            or isinstance(self.target_lease_expires_at, bool)
            or not isfinite(self.target_lease_expires_at)
            or self.target_lease_expires_at <= 0
        ):
            raise ValueError("inference target expiry must be a finite absolute instant")
        attempt_values = (self.inference_attempt_id, self.inference_fencing_token)
        if bool(attempt_values[0]) != bool(attempt_values[1]):
            raise ValueError("inference attempt identity and fence must be present together")
        if type(self.inference_fencing_token) is not int or self.inference_fencing_token < 0:
            raise ValueError("inference fencing token must be a non-negative integer")
        if self.inference_attempt_id and self.inference_fencing_token < 1:
            raise ValueError("active inference attempt fence must be positive")
        if self.route_schema_version != 2:
            raise ValueError("unsupported inference route schema")
        if type(self.tool_registry_revision) is not int or self.tool_registry_revision < 0:
            raise ValueError("tool registry revision must be a non-negative integer")


__all__ = [
    "ExecutionOperationContext",
    "ExecutionRecoveryFrontier",
    "InferenceCheckpointState",
    "InferenceCheckpointAttemptState",
    "InferenceCompleted",
    "InferenceDisposition",
    "InferenceStopped",
    "MutationResult",
    "MutationStatus",
]
