"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List

from mote.contracts.events._base import DurableFact as _DurableFact

if TYPE_CHECKING:
    pass

OUTPUT_CANDIDATE_RECEIVED = "output_candidate_received"

OUTPUT_VALIDATION_REJECTED = "output_validation_rejected"

OUTPUT_ACCEPTED = "output_accepted"

OUTPUT_MIGRATED = "output_migrated"

OUTPUT_COMMIT_STARTED = "output_commit_started"

OUTPUT_COMMITTED = "output_committed"

OUTPUT_PUBLICATION_QUEUED = "output_publication_queued"

OUTPUT_PUBLISHED = "output_published"

OUTPUT_SNAPSHOT = "output_snapshot"

OUTPUT_SNAPSHOT_INVALIDATED = "output_snapshot_invalidated"


@dataclass
class OutputCandidateReceivedEvent(_DurableFact):
    """A terminal output candidate entered the run-scoped output engine."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    representation: str = ""
    raw: Any = None
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED
    type: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED


@dataclass
class OutputValidationRejectedEvent(_DurableFact):
    """A candidate failed its output contract and was not accepted."""

    candidate_id: str = ""
    contract_id: str = ""
    issues: List[dict] = field(default_factory=list)
    correction_attempt: int = 0
    corrections_remaining: int = 0
    correction_allowed: bool = False
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_VALIDATION_REJECTED
    type: ClassVar[str] = OUTPUT_VALIDATION_REJECTED


@dataclass
class OutputAcceptedEvent(_DurableFact):
    """A candidate decoded and validated successfully, before durable commit."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: Any = None
    correction_attempts: int = 0
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_ACCEPTED
    type: ClassVar[str] = OUTPUT_ACCEPTED


@dataclass
class OutputCommitStartedEvent(_DurableFact):
    """Durable commit began for an already accepted output."""

    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMIT_STARTED
    type: ClassVar[str] = OUTPUT_COMMIT_STARTED


@dataclass
class OutputMigratedEvent(_DurableFact):
    """An explicit migration produced a candidate for the current contract."""

    candidate_id: str = ""
    source_contract_id: str = ""
    target_contract_id: str = ""
    target_schema_fingerprint: str = ""
    value: Any = None
    steps: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_MIGRATED
    type: ClassVar[str] = OUTPUT_MIGRATED


@dataclass
class OutputCommittedEvent(_DurableFact):
    """The accepted output and its transcript crossed the durability barrier."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: Any = None
    correction_attempts: int = 0
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMITTED
    type: ClassVar[str] = OUTPUT_COMMITTED


@dataclass
class OutputPublicationQueuedEvent(_DurableFact):
    """A committed output entered the durable publication outbox."""

    publication_id: str = ""
    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED
    type: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED


@dataclass
class OutputPublishedEvent(_DurableFact):
    """A committed output crossed the Role's outward publication boundary."""

    candidate_id: str = ""
    contract_id: str = ""
    publication_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_PUBLISHED
    type: ClassVar[str] = OUTPUT_PUBLISHED


@dataclass
class OutputSnapshotEvent:
    """A provisional structured value parsed from an in-flight LLM stream."""

    run_id: str = ""
    revision: int = 0
    schema_fingerprint: str = ""
    value: Any = None

    name: ClassVar[str] = OUTPUT_SNAPSHOT


@dataclass
class OutputSnapshotInvalidatedEvent:
    """A previously emitted provisional revision is no longer valid."""

    run_id: str = ""
    revision: int = 0
    reason: str = "stream_changed"

    name: ClassVar[str] = OUTPUT_SNAPSHOT_INVALIDATED
