"""Activity and task-progress event projections."""

from __future__ import annotations

from typing import Optional

from mote.contracts.events.task import ActivityCompletedEvent, ActivityStartedEvent, TaskProgressEvent
from mote.product.presentation.events.events import ActivityCompleted, ActivityStarted, TaskProgress, ViewEvent
from mote.product.presentation.input_events import PresentationInputEvent


def project_activity_event(event: PresentationInputEvent) -> Optional[list[ViewEvent]]:
    if isinstance(event, TaskProgressEvent):
        return [
            TaskProgress(
                stage=event.stage,
                status=event.status,
                detail=event.detail,
            )
        ]
    if isinstance(event, ActivityStartedEvent):
        return [
            ActivityStarted(
                activity_kind=event.activity_kind,
                label=event.label,
                topology=event.topology,
            )
        ]
    if isinstance(event, ActivityCompletedEvent):
        return [
            ActivityCompleted(
                outcome=event.outcome,
                node_states=event.node_states,
                summary=event.summary,
            )
        ]
    return None


__all__ = ["project_activity_event"]
