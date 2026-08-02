"""Canonical durable Workflow run state and typed commands."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from mote.contracts.clock import AbsoluteInstant
from mote.contracts.workflow import (
    WorkflowDefinitionId,
    WorkflowDefinitionSource,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
    WorkflowRunId,
    WorkflowRunReference,
    WorkflowTerminalResult,
)


class WorkflowRunPhase(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in {self.CANCELLED, self.SUCCEEDED, self.FAILED, self.TIMED_OUT}


class WorkflowPauseReason(str, Enum):
    EXTERNAL_INPUT = "external_input"
    APPROVAL = "approval"
    OPERATOR = "operator"


def _reject_non_finite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class WorkflowRunProjection:
    reference: WorkflowRunReference
    request_id: str
    provenance: WorkflowRunCreationProvenance
    access_grant: WorkflowRunAccessGrant
    revision: int
    phase: WorkflowRunPhase
    checkpoint_payload: str
    frontier: tuple[str, ...]
    deadline: AbsoluteInstant | None
    pause_reason: WorkflowPauseReason | None
    resume_nonce: str
    terminal_result: WorkflowTerminalResult | None
    definition_source: WorkflowDefinitionSource
    definition_digest: str
    initial_input_payload: str

    def __post_init__(self) -> None:
        _validate_activation(self.definition_digest, self.initial_input_payload)


@dataclass(frozen=True, slots=True)
class CreateWorkflowRun:
    request_id: str
    definition_id: WorkflowDefinitionId
    provenance: WorkflowRunCreationProvenance
    access_grant: WorkflowRunAccessGrant
    definition_source: WorkflowDefinitionSource
    definition_digest: str
    initial_input_payload: str
    checkpoint_payload: str = "{}"
    frontier: tuple[str, ...] = ()
    deadline: AbsoluteInstant | None = None

    def __post_init__(self) -> None:
        _validate_activation(self.definition_digest, self.initial_input_payload)

    @property
    def reference(self) -> WorkflowRunReference:
        material = f"{self.definition_id}\0{self.request_id}".encode("utf-8")
        return WorkflowRunReference(
            WorkflowRunId("wfr_" + hashlib.sha256(material).hexdigest()),
            self.definition_id,
        )


def _validate_activation(definition_digest: str, initial_input_payload: str) -> None:
    if (
        type(definition_digest) is not str
        or len(definition_digest) != 64
        or any(value not in "0123456789abcdef" for value in definition_digest)
    ):
        raise ValueError("Workflow definition digest is invalid")
    if type(initial_input_payload) is not str:
        raise ValueError("Workflow initial input payload must be a string")
    try:
        raw = json.loads(initial_input_payload, parse_constant=_reject_non_finite_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Workflow initial input payload is not JSON") from exc
    if type(raw) is not dict:
        raise ValueError("Workflow initial input payload must be an object")
    if json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False) != initial_input_payload:
        raise ValueError("Workflow initial input payload is not canonical JSON")


@dataclass(frozen=True, slots=True)
class WorkflowRunCommand:
    reference: WorkflowRunReference
    expected_revision: int


@dataclass(frozen=True, slots=True)
class PauseWorkflowRun(WorkflowRunCommand):
    reason: WorkflowPauseReason
    checkpoint_payload: str
    frontier: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckpointWorkflowRun(WorkflowRunCommand):
    checkpoint_payload: str
    frontier: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResumeWorkflowRun(WorkflowRunCommand):
    resume_nonce: str
    checkpoint_payload: str
    frontier: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettleWorkflowRun(WorkflowRunCommand):
    phase: WorkflowRunPhase
    terminal_result: WorkflowTerminalResult


__all__ = [
    "CheckpointWorkflowRun",
    "CreateWorkflowRun",
    "PauseWorkflowRun",
    "ResumeWorkflowRun",
    "SettleWorkflowRun",
    "WorkflowPauseReason",
    "WorkflowRunCommand",
    "WorkflowRunPhase",
    "WorkflowRunProjection",
]
