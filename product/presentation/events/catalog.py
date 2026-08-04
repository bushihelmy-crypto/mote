"""Closed Product generation of supported human-presentation events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.product.presentation.events import events as ev

VIEW_EVENT_GENERATION = "mote.view-events/v1"

VIEW_EVENT_TYPES: tuple[type[ev.ViewEvent], ...] = (
    ev.MessageBlockStarted,
    ev.MessageBlockDelta,
    ev.AttemptStreamCommitted,
    ev.AttemptStreamDiscarded,
    ev.AttemptStreamInterrupted,
    ev.MessageBlockCompleted,
    ev.ReasoningDelta,
    ev.OutputSnapshot,
    ev.OutputSnapshotInvalidated,
    ev.OutputCommitted,
    ev.ToolCallStarted,
    ev.ToolCallCompleted,
    ev.MediaBlock,
    ev.ArtifactBlock,
    ev.FileDiffBlock,
    ev.TaskProgress,
    ev.AsyncWorkObserved,
    ev.Notice,
    ev.ErrorRaised,
    ev.QuestionAsked,
    ev.ApprovalRequested,
    ev.UsageUpdated,
    ev.SessionListShown,
    ev.RetryStatus,
    ev.RuntimeDurabilityStatus,
    ev.TranscriptCleared,
    ev.SystemReminder,
    ev.ConversationCompacted,
    ev.ActivityStarted,
    ev.ActivityCompleted,
)


class ViewEventDisposition(StrEnum):
    HANDLED = "handled"
    INTENTIONALLY_OMITTED = "intentionally_omitted"


class UnsupportedViewEventError(TypeError):
    pass


@dataclass(frozen=True, slots=True)
class ViewEventDeclaration:
    generation: str
    kind: str
    event_type: type[ev.ViewEvent]


VIEW_EVENT_CATALOG = tuple(
    ViewEventDeclaration(VIEW_EVENT_GENERATION, event_type.kind, event_type) for event_type in VIEW_EVENT_TYPES
)
_BY_KIND = {declaration.kind: declaration for declaration in VIEW_EVENT_CATALOG}
if len(_BY_KIND) != len(VIEW_EVENT_CATALOG):
    raise RuntimeError("ViewEvent generation contains duplicate kind identities")


def require_view_event(event: ev.ViewEvent) -> ViewEventDeclaration:
    declaration = _BY_KIND.get(event.kind)
    if declaration is None or type(event) is not declaration.event_type:
        raise UnsupportedViewEventError(
            f"unsupported ViewEvent for {VIEW_EVENT_GENERATION}: {type(event).__name__}/{event.kind}"
        )
    return declaration


def adapter_disposition(event: ev.ViewEvent, handled_kinds: set[str] | frozenset[str]) -> ViewEventDisposition:
    declaration = require_view_event(event)
    return (
        ViewEventDisposition.HANDLED
        if declaration.kind in handled_kinds
        else ViewEventDisposition.INTENTIONALLY_OMITTED
    )


__all__ = [
    "UnsupportedViewEventError",
    "VIEW_EVENT_CATALOG",
    "VIEW_EVENT_GENERATION",
    "VIEW_EVENT_TYPES",
    "ViewEventDeclaration",
    "ViewEventDisposition",
    "adapter_disposition",
    "require_view_event",
]
