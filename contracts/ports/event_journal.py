"""Narrow durability contract for append-only recoverable facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Mapping, Optional, Protocol, Sequence

from mote.contracts.events.envelope import CorrelationId, EventEnvelope, EventId, EventType, JsonValue, StreamId


class EventJournalError(RuntimeError):
    """Base error for a journal operation that did not commit as requested."""


class StreamVersionConflict(EventJournalError):
    """The stream no longer has the caller's expected version."""

    def __init__(self, stream_id: StreamId, expected: int, actual: int) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"stream {stream_id!r} expected version {expected}, actual {actual}")


class JournalIntegrityError(EventJournalError):
    """The persisted stream cannot be proved complete and ordered."""


@dataclass(frozen=True)
class UncommittedFact:
    """Producer-owned fact data; sequence and recorded time are journal-owned."""

    event_id: EventId
    event_type: EventType
    schema_version: int
    occurred_at: datetime
    payload: Mapping[str, JsonValue]
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    turn_id: Optional[str] = None
    correlation_id: Optional[CorrelationId] = None
    causation_id: Optional[EventId] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class AppendResult:
    """The exact envelopes durably committed by one append."""

    stream_id: StreamId
    previous_version: int
    current_version: int
    envelopes: tuple[EventEnvelope[Mapping[str, JsonValue]], ...]

    @property
    def first_sequence(self) -> int:
        return self.envelopes[0].sequence

    @property
    def last_sequence(self) -> int:
        return self.envelopes[-1].sequence


@dataclass(frozen=True)
class VerificationIssue:
    line: int
    code: str
    detail: str


@dataclass(frozen=True)
class VerificationReport:
    stream_id: StreamId
    valid: bool
    record_count: int
    current_version: int
    last_checksum: Optional[str]
    issues: tuple[VerificationIssue, ...] = ()


class EventJournal(Protocol):
    """Crash-safe, optimistic-concurrency append and verified stream reads."""

    async def append(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
    ) -> AppendResult:
        ...

    def read(
        self,
        stream_id: StreamId,
        *,
        after: int = 0,
    ) -> AsyncIterator[EventEnvelope[Mapping[str, JsonValue]]]:
        ...

    async def verify(self, stream_id: StreamId) -> VerificationReport:
        ...


__all__ = [
    "AppendResult",
    "EventJournal",
    "EventJournalError",
    "JournalIntegrityError",
    "StreamVersionConflict",
    "UncommittedFact",
    "VerificationIssue",
    "VerificationReport",
]
