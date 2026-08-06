"""Explicit capability for committing recoverable session facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from mote.contracts.events.conversation import (
    ContextCompactedEvent,
    HistoryEditedEvent,
    MessageAppendedEvent,
    PromptRejectedEvent,
)
from mote.contracts.events.model import InferenceCheckpointConsumedEvent, ModelCallFinishedEvent, RoutingDecisionEvent
from mote.contracts.events.output import (
    FinalOutputCommittedEvent,
    OutputCandidateReceivedEvent,
    OutputMigratedEvent,
    OutputValidationRejectedEvent,
)
from mote.contracts.events.pending_act import PendingActEvent
from mote.contracts.events.session import TurnEndEvent
from mote.contracts.ports.events.journal import AppendResult, StreamWriterFence

RolloutSourceEvent: TypeAlias = (
    MessageAppendedEvent
    | ModelCallFinishedEvent
    | InferenceCheckpointConsumedEvent
    | ContextCompactedEvent
    | HistoryEditedEvent
    | OutputCandidateReceivedEvent
    | OutputValidationRejectedEvent
    | OutputMigratedEvent
    | FinalOutputCommittedEvent
    | PromptRejectedEvent
    | RoutingDecisionEvent
    | TurnEndEvent
    | PendingActEvent
)


class SessionFactSink(Protocol):
    async def commit_facts(self, events: tuple[RolloutSourceEvent, ...]) -> AppendResult: ...

    async def commit_fact(self, event: RolloutSourceEvent) -> AppendResult: ...


@dataclass(frozen=True, slots=True)
class GuardedSessionFactBatch:
    events: tuple[RolloutSourceEvent, ...]
    expected_stream_version: int
    writer: StreamWriterFence

    def __post_init__(self) -> None:
        if not self.events:
            raise ValueError("guarded session fact batch must not be empty")
        if type(self.expected_stream_version) is not int or self.expected_stream_version < 0:
            raise ValueError("expected stream version must be non-negative")
        if not isinstance(self.writer, StreamWriterFence):
            raise TypeError("guarded session fact writer has the wrong type")


class GuardedSessionFactSink(SessionFactSink, Protocol):
    async def commit_guarded(self, batch: GuardedSessionFactBatch) -> AppendResult: ...


__all__ = [
    "GuardedSessionFactBatch",
    "GuardedSessionFactSink",
    "RolloutSourceEvent",
    "SessionFactSink",
]
