"""Contracts for bounded consumption of committed journal facts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, NewType, Protocol

from mote.contracts.events.envelope import EventEnvelope, EventId, EventType, JsonValue, StreamId

SubscriptionIdentity = NewType("SubscriptionIdentity", str)

MAX_SUBSCRIPTION_CAPACITY = 65_536
MAX_RETRY_ATTEMPTS = 100
MAX_HANDLER_TIMEOUT_SECONDS = 300.0
MAX_DEAD_LETTER_ERROR_BYTES = 16 * 1024
_IDENTITY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


class Reliability(StrEnum):
    DURABLE = "durable"
    RELIABLE = "reliable"
    LIVE = "live"
    LOSSY = "lossy"


class Ordering(StrEnum):
    PER_STREAM = "per_stream"


class OverflowPolicy(StrEnum):
    BACKPRESSURE = "backpressure"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    COALESCE = "coalesce"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    attempt_timeout_seconds: float = 30.0
    initial_delay_seconds: float = 0.1
    maximum_delay_seconds: float = 5.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= MAX_RETRY_ATTEMPTS:
            raise ValueError("retry max_attempts is outside its bound")
        if (
            type(self.attempt_timeout_seconds) not in {int, float}
            or not 0 < self.attempt_timeout_seconds <= MAX_HANDLER_TIMEOUT_SECONDS
        ):
            raise ValueError("handler attempt timeout is outside its bound")
        if type(self.initial_delay_seconds) not in {int, float} or self.initial_delay_seconds < 0:
            raise ValueError("retry initial delay is invalid")
        if (
            type(self.maximum_delay_seconds) not in {int, float}
            or self.maximum_delay_seconds < self.initial_delay_seconds
        ):
            raise ValueError("retry maximum delay is invalid")
        if type(self.jitter_ratio) not in {int, float} or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("retry jitter ratio is invalid")


@dataclass(frozen=True)
class CheckpointPolicy:
    persist_every: int = 1

    def __post_init__(self) -> None:
        if type(self.persist_every) is not int or self.persist_every < 1:
            raise ValueError("checkpoint persist_every must be positive")


@dataclass(frozen=True)
class EventFilter:
    """Serializable closed filter; empty dimensions mean no restriction."""

    event_types: frozenset[EventType] = field(default_factory=frozenset)
    stream_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(type(prefix) is not str or not prefix for prefix in self.stream_prefixes):
            raise ValueError("event filter stream prefix is invalid")

    def matches(self, envelope: EventEnvelope[object]) -> bool:
        return self.matches_stream(envelope.stream_id) and self.matches_event_type(envelope.event_type)

    def matches_stream(self, stream_id: StreamId) -> bool:
        return not self.stream_prefixes or any(str(stream_id).startswith(prefix) for prefix in self.stream_prefixes)

    def matches_event_type(self, event_type: EventType) -> bool:
        return not self.event_types or event_type in self.event_types


@dataclass(frozen=True)
class SubscriptionSpec:
    identity: SubscriptionIdentity
    event_filter: EventFilter
    reliability: Reliability
    ordering: Ordering
    capacity: int
    overflow: OverflowPolicy
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    checkpoint: CheckpointPolicy = field(default_factory=CheckpointPolicy)

    def __post_init__(self) -> None:
        if type(self.identity) is not str or _IDENTITY_PATTERN.fullmatch(self.identity) is None:
            raise ValueError("subscription identity must be stable and namespaced")
        if type(self.capacity) is not int or not 1 <= self.capacity <= MAX_SUBSCRIPTION_CAPACITY:
            raise ValueError("subscription capacity is outside its bound")
        if self.ordering is not Ordering.PER_STREAM:
            raise ValueError("unsupported subscription ordering")
        if self.reliability in {Reliability.DURABLE, Reliability.RELIABLE}:
            if self.overflow is not OverflowPolicy.BACKPRESSURE:
                raise ValueError("recoverable subscriptions must use backpressure")
        elif self.overflow is OverflowPolicy.BACKPRESSURE:
            raise ValueError("live and lossy subscriptions must not backpressure")


@dataclass(frozen=True)
class SubscriptionCheckpoint:
    identity: SubscriptionIdentity
    stream_id: StreamId
    sequence: int

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("subscription checkpoint sequence is invalid")


@dataclass(frozen=True)
class DeadLetterEntry:
    subscription: SubscriptionIdentity
    envelope: EventEnvelope[Mapping[str, JsonValue]]
    attempts: int
    error: str
    first_failed_at: datetime
    last_failed_at: datetime

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or not 1 <= self.attempts <= MAX_RETRY_ATTEMPTS:
            raise ValueError("dead-letter attempts are outside their bound")
        if type(self.error) is not str or not self.error:
            raise ValueError("dead-letter error must be non-empty")
        if len(self.error.encode("utf-8")) > MAX_DEAD_LETTER_ERROR_BYTES:
            raise ValueError("dead-letter error exceeds its byte bound")
        if self.first_failed_at.tzinfo is None or self.last_failed_at.tzinfo is None:
            raise ValueError("dead-letter timestamps must be timezone-aware")
        if self.last_failed_at < self.first_failed_at:
            raise ValueError("dead-letter failure timestamps are reversed")

    @property
    def stream_id(self) -> StreamId:
        return self.envelope.stream_id

    @property
    def sequence(self) -> int:
        return self.envelope.sequence

    @property
    def event_id(self) -> EventId:
        return self.envelope.event_id


class CommittedEventHandler(Protocol):
    async def handle(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> None:
        ...


class SubscriptionStateStore(Protocol):
    """Durable cursor and poison-event state for all subscriptions.

    ``quarantine`` must persist the dead letter and its checkpoint atomically.
    """

    async def load(
        self,
        identity: SubscriptionIdentity,
        stream_id: StreamId,
    ) -> int:
        ...

    async def save(self, checkpoint: SubscriptionCheckpoint) -> None:
        ...

    async def quarantine(
        self,
        entry: DeadLetterEntry,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        ...


class ManagedSubscriptionStateStore(SubscriptionStateStore, Protocol):
    """Explicit lifecycle for the process-owned subscription state backend."""

    async def aopen(self) -> None:
        ...

    async def aclose(self) -> None:
        ...


__all__ = [
    "CheckpointPolicy",
    "CommittedEventHandler",
    "DeadLetterEntry",
    "EventFilter",
    "MAX_DEAD_LETTER_ERROR_BYTES",
    "MAX_HANDLER_TIMEOUT_SECONDS",
    "MAX_RETRY_ATTEMPTS",
    "MAX_SUBSCRIPTION_CAPACITY",
    "ManagedSubscriptionStateStore",
    "Ordering",
    "OverflowPolicy",
    "Reliability",
    "RetryPolicy",
    "SubscriptionCheckpoint",
    "SubscriptionIdentity",
    "SubscriptionSpec",
    "SubscriptionStateStore",
]
