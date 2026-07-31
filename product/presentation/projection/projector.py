"""Single narrow-waist fold from Agent events to human View events."""

from __future__ import annotations

from mote.contracts.events.tool import (
    TOOL_CALL_FINISHED,
    TOOL_INVOCATION_STARTED,
    ToolCallFinishedEvent,
    ToolInvocationStartedEvent,
)
from mote.product.presentation.events.events import ViewEvent
from mote.product.presentation.input_events import PresentationInputEvent
from mote.product.presentation.projection.handlers.activity import project_activity_event
from mote.product.presentation.projection.handlers.message import MessageProjectionState
from mote.product.presentation.projection.handlers.output import project_output_event
from mote.product.presentation.projection.handlers.system import project_system_event
from mote.product.presentation.projection.handlers.tool_finished import project_tool_finished
from mote.product.presentation.projection.handlers.tool_started import project_tool_started


class ViewProjector:
    """Dispatch event families and propagate execution lineage."""

    def __init__(self) -> None:
        self._messages = MessageProjectionState()

    def project(self, event: PresentationInputEvent) -> list[ViewEvent]:
        output = self._project(event)
        scope = event.scope if isinstance(event, (ToolInvocationStartedEvent, ToolCallFinishedEvent)) else ()
        if scope:
            for view_event in output:
                if not view_event.scope:
                    view_event.scope = scope
        return output

    def _project(self, event: PresentationInputEvent) -> list[ViewEvent]:
        message_output = self._messages.project(event)
        if message_output is not None:
            return message_output
        for handler in (
            project_output_event,
            project_activity_event,
            project_system_event,
        ):
            output = handler(event)
            if output is not None:
                return output
        if event.name == TOOL_INVOCATION_STARTED:
            if not isinstance(event, ToolInvocationStartedEvent):
                return []
            started = project_tool_started(event)
            return [started] if started is not None else []
        if event.name == TOOL_CALL_FINISHED:
            if not isinstance(event, ToolCallFinishedEvent):
                return []
            return project_tool_finished(event)
        return []


__all__ = ["ViewProjector"]
