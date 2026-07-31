"""Fenced transaction contracts for a single agent run frontier."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True, slots=True)
class MutationResult:
    status: MutationStatus
    revision: int | None = None
    reference_id: str = ""
    reason: str = ""


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
    staged_output_id: str = ""
    terminal_committed: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class InferenceCheckpointState:
    model_call_id: str
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


__all__ = [
    "ExecutionOperationContext",
    "ExecutionRecoveryFrontier",
    "InferenceCheckpointState",
    "MutationResult",
    "MutationStatus",
]
