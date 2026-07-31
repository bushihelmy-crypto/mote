"""Closed input edge for the human presentation projector."""

from __future__ import annotations

from typing import TypeGuard

from mote.contracts.events.agent import BudgetEvent
from mote.contracts.events.conversation import ContextCompactedEvent, MessageAppendedEvent
from mote.contracts.events.model import (
    LLMStreamCommittedEvent,
    LLMStreamDeltaEvent,
    LLMStreamDiscardedEvent,
    LLMStreamEndEvent,
    LLMStreamInterruptedEvent,
    ModelAttemptFinishedEvent,
)
from mote.contracts.events.output import OutputCommittedEvent, OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.contracts.events.session import RuntimeDurabilityChangedEvent
from mote.contracts.events.task import ActivityCompletedEvent, ActivityStartedEvent, TaskProgressEvent
from mote.contracts.events.tool import ToolCallFinishedEvent, ToolInvocationStartedEvent

PresentationInputEvent = (
    BudgetEvent
    | ContextCompactedEvent
    | MessageAppendedEvent
    | LLMStreamCommittedEvent
    | LLMStreamDeltaEvent
    | LLMStreamDiscardedEvent
    | LLMStreamEndEvent
    | LLMStreamInterruptedEvent
    | ModelAttemptFinishedEvent
    | OutputCommittedEvent
    | OutputSnapshotEvent
    | OutputSnapshotInvalidatedEvent
    | RuntimeDurabilityChangedEvent
    | ActivityCompletedEvent
    | ActivityStartedEvent
    | TaskProgressEvent
    | ToolCallFinishedEvent
    | ToolInvocationStartedEvent
)

_PRESENTATION_INPUT_TYPES = (
    BudgetEvent,
    ContextCompactedEvent,
    MessageAppendedEvent,
    LLMStreamCommittedEvent,
    LLMStreamDeltaEvent,
    LLMStreamDiscardedEvent,
    LLMStreamEndEvent,
    LLMStreamInterruptedEvent,
    ModelAttemptFinishedEvent,
    OutputCommittedEvent,
    OutputSnapshotEvent,
    OutputSnapshotInvalidatedEvent,
    RuntimeDurabilityChangedEvent,
    ActivityCompletedEvent,
    ActivityStartedEvent,
    TaskProgressEvent,
    ToolCallFinishedEvent,
    ToolInvocationStartedEvent,
)


def is_presentation_input(event: object) -> TypeGuard[PresentationInputEvent]:
    """Narrow the heterogeneous telemetry spine at the Product edge."""

    return isinstance(event, _PRESENTATION_INPUT_TYPES)


__all__ = ["PresentationInputEvent", "is_presentation_input"]
