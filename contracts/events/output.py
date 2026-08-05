"""Domain-owned event contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from mote.contracts.conversation import Message
from mote.contracts.conversation.codec import dump_message, load_message
from mote.contracts.events._base import DurableFact as _DurableFact
from mote.contracts.events.envelope import JsonValue, freeze_json

OUTPUT_CANDIDATE_RECEIVED = "output_candidate_received"

OUTPUT_VALIDATION_REJECTED = "output_validation_rejected"

OUTPUT_MIGRATED = "output_migrated"
FINAL_OUTPUT_COMMITTED = "final_output_committed.v2"

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


def _exact(payload: dict[str, JsonValue], fields: set[str], *, owner: str) -> dict[str, JsonValue]:
    if set(payload) != fields:
        raise ValueError(f"{owner} payload fields are not canonical")
    return payload


def _text(payload: dict[str, JsonValue], name: str, owner: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"{owner}.{name} must be a string")
    return value


def _integer(payload: dict[str, JsonValue], name: str, owner: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"{owner}.{name} must be an integer")
    return value


def _boolean(payload: dict[str, JsonValue], name: str, owner: str) -> bool:
    value = payload[name]
    if type(value) is not bool:
        raise TypeError(f"{owner}.{name} must be a boolean")
    return value


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

    def payload(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": self.candidate_id,
            "contract_id": self.contract_id,
            "schema_fingerprint": self.schema_fingerprint,
            "representation": self.representation,
            "raw": self.raw,
            "run_id": self.run_id,
            "run_kind": self.run_kind,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "OutputCandidateReceivedEvent":
        values = _exact(
            payload,
            {"candidate_id", "contract_id", "schema_fingerprint", "representation", "raw", "run_id", "run_kind"},
            owner=cls.__name__,
        )
        return cls(
            candidate_id=_text(values, "candidate_id", cls.__name__),
            contract_id=_text(values, "contract_id", cls.__name__),
            schema_fingerprint=_text(values, "schema_fingerprint", cls.__name__),
            representation=_text(values, "representation", cls.__name__),
            raw=values["raw"],
            run_id=_text(values, "run_id", cls.__name__),
            run_kind=_text(values, "run_kind", cls.__name__),
        )


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

    def payload(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": self.candidate_id,
            "contract_id": self.contract_id,
            "issues": cast(JsonValue, list(self.issues)),
            "correction_attempt": self.correction_attempt,
            "corrections_remaining": self.corrections_remaining,
            "correction_allowed": self.correction_allowed,
            "validator_provenance": cast(JsonValue, list(self.validator_provenance)),
            "run_id": self.run_id,
            "run_kind": self.run_kind,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "OutputValidationRejectedEvent":
        values = _exact(
            payload,
            {
                "candidate_id",
                "contract_id",
                "issues",
                "correction_attempt",
                "corrections_remaining",
                "correction_allowed",
                "validator_provenance",
                "run_id",
                "run_kind",
            },
            owner=cls.__name__,
        )
        return cls(
            candidate_id=_text(values, "candidate_id", cls.__name__),
            contract_id=_text(values, "contract_id", cls.__name__),
            issues=_freeze_records(values["issues"], field_name="issues"),
            correction_attempt=_integer(values, "correction_attempt", cls.__name__),
            corrections_remaining=_integer(values, "corrections_remaining", cls.__name__),
            correction_allowed=_boolean(values, "correction_allowed", cls.__name__),
            validator_provenance=_freeze_records(values["validator_provenance"], field_name="validator_provenance"),
            run_id=_text(values, "run_id", cls.__name__),
            run_kind=_text(values, "run_kind", cls.__name__),
        )


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

    def payload(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": self.candidate_id,
            "source_contract_id": self.source_contract_id,
            "target_contract_id": self.target_contract_id,
            "target_schema_fingerprint": self.target_schema_fingerprint,
            "value": self.value,
            "steps": cast(JsonValue, list(self.steps)),
            "run_id": self.run_id,
            "run_kind": self.run_kind,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "OutputMigratedEvent":
        values = _exact(
            payload,
            {
                "candidate_id",
                "source_contract_id",
                "target_contract_id",
                "target_schema_fingerprint",
                "value",
                "steps",
                "run_id",
                "run_kind",
            },
            owner=cls.__name__,
        )
        return cls(
            candidate_id=_text(values, "candidate_id", cls.__name__),
            source_contract_id=_text(values, "source_contract_id", cls.__name__),
            target_contract_id=_text(values, "target_contract_id", cls.__name__),
            target_schema_fingerprint=_text(values, "target_schema_fingerprint", cls.__name__),
            value=values["value"],
            steps=_freeze_records(values["steps"], field_name="steps"),
            run_id=_text(values, "run_id", cls.__name__),
            run_kind=_text(values, "run_kind", cls.__name__),
        )


@dataclass(frozen=True)
class FinalOutputCommittedEvent(_DurableFact):
    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: JsonValue = None
    message: Message | None = None
    correction_attempts: int = 0
    validator_provenance: tuple[Mapping[str, JsonValue], ...] = ()
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = FINAL_OUTPUT_COMMITTED
    type: ClassVar[str] = FINAL_OUTPUT_COMMITTED

    def __post_init__(self) -> None:
        if self.message is None:
            raise ValueError("final output commit requires its terminal message")
        object.__setattr__(self, "value", freeze_json(self.value, path="committed output value"))
        object.__setattr__(
            self,
            "validator_provenance",
            _freeze_records(self.validator_provenance, field_name="output validator provenance"),
        )

    def payload(self) -> dict[str, JsonValue]:
        assert self.message is not None
        return {
            "candidate_id": self.candidate_id,
            "contract_id": self.contract_id,
            "schema_fingerprint": self.schema_fingerprint,
            "value": self.value,
            "message": dump_message(self.message),
            "correction_attempts": self.correction_attempts,
            "validator_provenance": cast(JsonValue, list(self.validator_provenance)),
            "run_id": self.run_id,
            "run_kind": self.run_kind,
            "fencing_token": self.fencing_token,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "FinalOutputCommittedEvent":
        fields = {
            "candidate_id",
            "contract_id",
            "schema_fingerprint",
            "value",
            "message",
            "correction_attempts",
            "validator_provenance",
            "run_id",
            "run_kind",
            "fencing_token",
        }
        values = _exact(payload, fields, owner=cls.__name__)
        return cls(
            candidate_id=_text(values, "candidate_id", cls.__name__),
            contract_id=_text(values, "contract_id", cls.__name__),
            schema_fingerprint=_text(values, "schema_fingerprint", cls.__name__),
            value=values["value"],
            message=load_message(_text(values, "message", cls.__name__)),
            correction_attempts=_integer(values, "correction_attempts", cls.__name__),
            validator_provenance=_freeze_records(values["validator_provenance"], field_name="validator_provenance"),
            run_id=_text(values, "run_id", cls.__name__),
            run_kind=_text(values, "run_kind", cls.__name__),
            fencing_token=_integer(values, "fencing_token", cls.__name__),
        )


@dataclass
class OutputSnapshotEvent:
    """A provisional structured value parsed from an in-flight LLM stream."""

    run_id: str = ""
    revision: int = 0
    schema_fingerprint: str = ""
    value: JsonValue = None

    name: ClassVar[str] = OUTPUT_SNAPSHOT

    def __post_init__(self) -> None:
        self.value = freeze_json(self.value, path="output snapshot value")


@dataclass
class OutputSnapshotInvalidatedEvent:
    """A previously emitted provisional revision is no longer valid."""

    run_id: str = ""
    revision: int = 0
    reason: str = "stream_changed"

    name: ClassVar[str] = OUTPUT_SNAPSHOT_INVALIDATED
