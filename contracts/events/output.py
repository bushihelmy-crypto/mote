"""Domain-owned event contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from mote.contracts.events._base import DurableFact as _DurableFact
from mote.contracts.events.envelope import JsonValue, freeze_json

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


def _freeze_records(value: object, *, field_name: str) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{field_name} must be a sequence of JSON objects")
    records: list[Mapping[str, JsonValue]] = []
    for index, item in enumerate(value):
        frozen = freeze_json(item, path=f"{field_name}[{index}]")
        if not isinstance(frozen, Mapping):
            raise TypeError(f"{field_name}[{index}] must be a JSON object")
        records.append(cast(Mapping[str, JsonValue], frozen))
    return tuple(records)


@dataclass(frozen=True)
class OutputCandidateReceivedEvent(_DurableFact):
    """A terminal output candidate entered the run-scoped output engine."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    representation: str = ""
    raw: JsonValue = None
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED
    type: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_json(self.raw, path="output candidate raw"))


@dataclass(frozen=True)
class OutputValidationRejectedEvent(_DurableFact):
    """A candidate failed its output contract and was not accepted."""

    candidate_id: str = ""
    contract_id: str = ""
    issues: tuple[Mapping[str, JsonValue], ...] = ()
    correction_attempt: int = 0
    corrections_remaining: int = 0
    correction_allowed: bool = False
    validator_provenance: tuple[Mapping[str, JsonValue], ...] = ()
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_VALIDATION_REJECTED
    type: ClassVar[str] = OUTPUT_VALIDATION_REJECTED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            _freeze_records(self.issues, field_name="output validation issues"),
        )
        object.__setattr__(
            self,
            "validator_provenance",
            _freeze_records(
                self.validator_provenance,
                field_name="output validator provenance",
            ),
        )


@dataclass(frozen=True)
class OutputAcceptedEvent(_DurableFact):
    """A candidate decoded and validated successfully, before durable commit."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: JsonValue = None
    correction_attempts: int = 0
    validator_provenance: tuple[Mapping[str, JsonValue], ...] = ()
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_ACCEPTED
    type: ClassVar[str] = OUTPUT_ACCEPTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_json(self.value, path="accepted output value"))
        object.__setattr__(
            self,
            "validator_provenance",
            _freeze_records(
                self.validator_provenance,
                field_name="output validator provenance",
            ),
        )


@dataclass(frozen=True)
class OutputCommitStartedEvent(_DurableFact):
    """Durable commit began for an already accepted output."""

    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMIT_STARTED
    type: ClassVar[str] = OUTPUT_COMMIT_STARTED


@dataclass(frozen=True)
class OutputMigratedEvent(_DurableFact):
    """An explicit migration produced a candidate for the current contract."""

    candidate_id: str = ""
    source_contract_id: str = ""
    target_contract_id: str = ""
    target_schema_fingerprint: str = ""
    value: JsonValue = None
    steps: tuple[Mapping[str, JsonValue], ...] = ()
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_MIGRATED
    type: ClassVar[str] = OUTPUT_MIGRATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_json(self.value, path="migrated output value"))
        object.__setattr__(
            self,
            "steps",
            _freeze_records(self.steps, field_name="output migration steps"),
        )


@dataclass(frozen=True)
class OutputCommittedEvent(_DurableFact):
    """The accepted output and its transcript crossed the durability barrier."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: JsonValue = None
    correction_attempts: int = 0
    validator_provenance: tuple[Mapping[str, JsonValue], ...] = ()
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMITTED
    type: ClassVar[str] = OUTPUT_COMMITTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_json(self.value, path="committed output value"))
        object.__setattr__(
            self,
            "validator_provenance",
            _freeze_records(
                self.validator_provenance,
                field_name="output validator provenance",
            ),
        )


@dataclass(frozen=True)
class OutputPublicationQueuedEvent(_DurableFact):
    """A committed output entered the durable publication outbox."""

    publication_id: str = ""
    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED
    type: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED


@dataclass(frozen=True)
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
