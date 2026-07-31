"""Operational application-composition lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ApplicationActivationRequested:
    token_fingerprint: str
    reload_sequence: int
    source_revision: str
    name: ClassVar[str] = "application_activation_requested"


@dataclass(frozen=True, slots=True)
class ApplicationActivationCommitted:
    application_generation_id: str
    runtime_generation_id: str
    topology_revision: str
    source_revision: str
    name: ClassVar[str] = "application_activation_committed"


@dataclass(frozen=True, slots=True)
class ApplicationActivationRejected:
    reload_sequence: int
    reason_code: str
    name: ClassVar[str] = "application_activation_rejected"


@dataclass(frozen=True, slots=True)
class ApplicationActivationStale:
    candidate_sequence: int
    latest_sequence: int
    source_revision_mismatch: bool
    name: ClassVar[str] = "application_activation_stale"


@dataclass(frozen=True, slots=True)
class ApplicationActivationCasConflict:
    expected_generation_id: str
    current_generation_id: str
    name: ClassVar[str] = "application_activation_cas_conflict"


@dataclass(frozen=True, slots=True)
class ApplicationReadinessFailed:
    component_kind: str
    error_code: str
    name: ClassVar[str] = "application_readiness_failed"


@dataclass(frozen=True, slots=True)
class RetiredGenerationCapacityReached:
    retired_count: int
    limit: int
    oldest_age_bucket: str
    name: ClassVar[str] = "retired_generation_capacity_reached"


@dataclass(frozen=True, slots=True)
class GenerationDrainCompleted:
    generation_id: str
    duration_bucket: str
    name: ClassVar[str] = "generation_drain_completed"


@dataclass(frozen=True, slots=True)
class GenerationDrainTimedOut:
    generation_id: str
    lease_count: int
    age_bucket: str
    name: ClassVar[str] = "generation_drain_timed_out"


@dataclass(frozen=True, slots=True)
class InferenceTargetExpired:
    target_state: str
    age_bucket: str
    name: ClassVar[str] = "inference_target_expired"


@dataclass(frozen=True, slots=True)
class InferenceTargetCapacityReached:
    target_count: int
    limit: int
    name: ClassVar[str] = "inference_target_capacity_reached"


@dataclass(frozen=True, slots=True)
class CompositionCloseFailed:
    resource_kind: str
    resource_identity: str
    error_code: str
    error_count: int
    name: ClassVar[str] = "composition_close_failed"


@dataclass(frozen=True, slots=True)
class ApplicationShutdownTimedOut:
    generation_count: int
    lease_count: int
    oldest_age_bucket: str
    name: ClassVar[str] = "application_shutdown_timed_out"


__all__ = [
    name
    for name in globals()
    if name.startswith("Application")
    or name.startswith("Generation")
    or name.startswith("Retired")
    or name.startswith("Inference")
    or name.startswith("Composition")
]
