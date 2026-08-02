"""Explicit capability for committing recoverable session facts."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from mote.contracts.events.conversation import (
    ContextCompactedEvent,
    HistoryEditedEvent,
    MessageAppendedEvent,
    PromptRejectedEvent,
)
from mote.contracts.events.model import ModelCallFinishedEvent, RoutingDecisionEvent
from mote.contracts.events.output import (
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
)
from mote.contracts.events.session import TurnEndEvent
from mote.contracts.ports.events.journal import AppendResult

RolloutSourceEvent: TypeAlias = (
    MessageAppendedEvent
    | ModelCallFinishedEvent
    | ContextCompactedEvent
    | HistoryEditedEvent
    | OutputCandidateReceivedEvent
    | OutputValidationRejectedEvent
    | OutputAcceptedEvent
    | OutputCommitStartedEvent
    | OutputMigratedEvent
    | OutputCommittedEvent
    | OutputPublicationQueuedEvent
    | OutputPublishedEvent
    | PromptRejectedEvent
    | RoutingDecisionEvent
    | TurnEndEvent
)


class SessionFactSink(Protocol):
    async def commit_fact(self, event: RolloutSourceEvent) -> AppendResult: ...


__all__ = ["RolloutSourceEvent", "SessionFactSink"]
