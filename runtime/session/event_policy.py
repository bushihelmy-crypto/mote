"""Session-owned policy for observation facts eligible for rollout persistence."""
from __future__ import annotations

from typing import TypeAlias

from mote.contracts.events.types import (
    ContextCompactedEvent,
    HistoryEditedEvent,
    MessageAppendedEvent,
    ModelCallFinishedEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    PromptRejectedEvent,
    RoutingDecisionEvent,
    TurnEndEvent,
)

EventType: TypeAlias = type[object]

# Persistence is a session policy, orthogonal to whether CLI/logging/telemetry
# also consume the same fact.
ROLLOUT_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        MessageAppendedEvent,
        ModelCallFinishedEvent,
        ContextCompactedEvent,
        HistoryEditedEvent,
        OutputCandidateReceivedEvent,
        OutputValidationRejectedEvent,
        OutputAcceptedEvent,
        OutputCommitStartedEvent,
        OutputMigratedEvent,
        OutputCommittedEvent,
        OutputPublicationQueuedEvent,
        OutputPublishedEvent,
        PromptRejectedEvent,
        RoutingDecisionEvent,
        TurnEndEvent,
    }
)


def is_rollout_event(event: object) -> bool:
    """Whether the recorder owns a persistence projection for ``event``."""
    return type(event) in ROLLOUT_EVENT_TYPES


__all__ = ["ROLLOUT_EVENT_TYPES", "is_rollout_event"]
