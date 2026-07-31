"""Session-owned policy for observation facts eligible for rollout persistence."""
from __future__ import annotations

from typing import TypeAlias

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
