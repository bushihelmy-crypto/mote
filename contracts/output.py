"""Stable, versionable data contracts for typed run outputs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeAlias, TypeVar, Union

OutputT = TypeVar("OutputT")


class OutputBindingKind(str, Enum):
    TEXT = "text"
    NATIVE_SCHEMA = "native_schema"
    NATIVE_TOOL = "native_tool"
    PROMPTED_JSON = "prompted_json"


class OutputLifecycleState(str, Enum):
    IDLE = "idle"
    CANDIDATE_RECEIVED = "candidate_received"
    AWAITING_CORRECTION = "awaiting_correction"
    CORRECTION_EXHAUSTED = "correction_exhausted"
    ACCEPTED = "accepted"
    COMMIT_STARTED = "commit_started"
    COMMITTED = "committed"
    PUBLICATION_QUEUED = "publication_queued"


class RunKind(str, Enum):
    AGENT = "agent"
    GRAPH = "graph"


class RunRejectionKind(str, Enum):
    PROMPT_ADMISSION = "prompt_admission"


class ValidationStage(str, Enum):
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    POLICY = "policy"


class ValidatorEffect(str, Enum):
    PURE = "pure"
    READ_EXTERNAL = "read_external"


class Determinism(str, Enum):
    DETERMINISTIC = "deterministic"
    EXTERNAL_STATE = "external_state"


@dataclass(frozen=True)
class OutputContractId:
    namespace: str
    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.namespace}.{self.name}@{self.version}"


@dataclass(frozen=True)
class SchemaDocument:
    dialect: str
    canonical: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True)
class OutputBinding:
    kind: OutputBindingKind
    tool_name: str = ""


@dataclass(frozen=True)
class OutputRepresentationCapabilities:
    """Wire representations implemented by one concrete command channel."""

    supports_text: bool = True
    supports_native_schema: bool = False
    supports_semantic_tool: bool = False
    supports_prompted_json: bool = False
    protocol: str = ""
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class OutputBindingDecision:
    """Selected output representation plus explicit downgrade provenance."""

    binding: OutputBinding
    downgrade_reasons: tuple[str, ...] = ()
    capabilities: OutputRepresentationCapabilities = OutputRepresentationCapabilities()


@dataclass(frozen=True)
class ValidationIssue:
    path: tuple[str | int, ...]
    code: str
    message: str


class OutputDecodeError(ValueError):
    """A decoder rejected candidate data with normalized structural issues."""

    def __init__(self, issues: tuple[ValidationIssue, ...]):
        super().__init__("output candidate failed structural validation")
        self.issues = issues


@dataclass(frozen=True)
class ValidationContext:
    candidate_id: str
    contract_id: str
    correction_attempts: int = 0


@dataclass(frozen=True)
class ValidatorProvenance:
    name: str
    version: str
    stage: str
    effect: str
    determinism: str
    decision: str


@dataclass(frozen=True)
class Accept(Generic[OutputT]):
    value: OutputT


@dataclass(frozen=True)
class Corrected(Generic[OutputT]):
    value: OutputT
    note: str = ""


@dataclass(frozen=True)
class Reject:
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class RetryLater:
    reason: str
    retry_after_seconds: float | None = None


ValidatorDecision: TypeAlias = Union[
    Accept[OutputT],
    Corrected[OutputT],
    Reject,
    RetryLater,
]


@dataclass(frozen=True)
class CorrectionFeedback:
    summary: str
    issues: tuple[ValidationIssue, ...]
    candidate_id: str = ""
    correction_attempt: int = 0
    corrections_remaining: int = 0


@dataclass(frozen=True)
class OutputEvaluation(Generic[OutputT]):
    accepted: bool
    candidate_id: str = ""
    value: OutputT | None = None
    issues: tuple[ValidationIssue, ...] = ()
    correction_attempt: int = 0
    corrections_remaining: int = 0
    correction_allowed: bool = False
    max_corrections: int = 0

    def feedback(self, candidate_id: str = "") -> CorrectionFeedback:
        return CorrectionFeedback(
            summary="The final output did not satisfy its output contract. Correct the listed issues and submit again.",
            issues=self.issues,
            candidate_id=candidate_id or self.candidate_id,
            correction_attempt=self.correction_attempt,
            corrections_remaining=self.corrections_remaining,
        )


@dataclass(frozen=True)
class CommittedOutput(Generic[OutputT]):
    candidate_id: str
    contract_id: str
    schema_fingerprint: str
    value: OutputT
    correction_attempts: int = 0
    validator_provenance: tuple[ValidatorProvenance, ...] = ()
    run_id: str = ""
    run_kind: RunKind = RunKind.AGENT
    fencing_token: int = 0


@dataclass(frozen=True)
class TranscriptRef:
    session_id: str
    terminal_message_id: str = ""


@dataclass(frozen=True)
class RunResult(Generic[OutputT]):
    """Successful run outcome; construction requires a committed output record."""

    output: OutputT
    output_record: CommittedOutput[OutputT]
    transcript: TranscriptRef
    run_id: str = ""


@dataclass(frozen=True)
class RunRejected:
    """A request rejected before execution crossed its admission boundary."""

    kind: RunRejectionKind
    reason: str
    transcript: TranscriptRef
    terminate: bool = False


RunOutcome: TypeAlias = Union[RunResult[OutputT], RunRejected]


__all__ = [
    "Accept",
    "CommittedOutput",
    "Corrected",
    "CorrectionFeedback",
    "Determinism",
    "OutputBinding",
    "OutputBindingDecision",
    "OutputBindingKind",
    "OutputContractId",
    "OutputDecodeError",
    "OutputEvaluation",
    "OutputLifecycleState",
    "OutputRepresentationCapabilities",
    "Reject",
    "RetryLater",
    "RunKind",
    "RunOutcome",
    "RunRejectionKind",
    "RunRejected",
    "RunResult",
    "SchemaDocument",
    "TranscriptRef",
    "ValidationContext",
    "ValidationIssue",
    "ValidationStage",
    "ValidatorEffect",
    "ValidatorDecision",
    "ValidatorProvenance",
]
