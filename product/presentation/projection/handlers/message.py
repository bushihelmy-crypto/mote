"""Stateful message and model-stream event projection."""

from __future__ import annotations

from typing import Optional

from mote.contracts.events.conversation import MessageAppendedEvent, TurnContextCollectedEvent
from mote.contracts.events.model import (
    LLMStreamCommittedEvent,
    LLMStreamDeltaEvent,
    LLMStreamDiscardedEvent,
    LLMStreamEndEvent,
    LLMStreamInterruptedEvent,
)
from mote.product.presentation.events.events import (
    AttemptStreamCommitted,
    AttemptStreamDiscarded,
    AttemptStreamInterrupted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    SystemReminder,
    SystemReminderLifetime,
    ViewEvent,
)
from mote.product.presentation.input_events import PresentationInputEvent
from mote.product.presentation.projection.reminders import _is_system_reminder, _summarize_reminder


class MessageProjectionState:
    """Own the streaming frontier for message-family projection."""

    def __init__(self) -> None:
        self._streaming = False

    def project(self, event: PresentationInputEvent) -> Optional[list[ViewEvent]]:
        if isinstance(event, LLMStreamDeltaEvent):
            token = event.token
            if not token:
                return []
            if event.provisional:
                return [
                    MessageBlockDelta(
                        text=token,
                        model_call_id=event.model_call_id,
                        attempt_id=event.attempt_id,
                        sequence=event.sequence,
                        provisional=True,
                    )
                ]
            output: list[ViewEvent] = []
            if not self._streaming:
                self._streaming = True
                output.append(MessageBlockStarted(role="assistant"))
            output.append(MessageBlockDelta(text=token))
            return output
        if isinstance(event, LLMStreamCommittedEvent):
            self._streaming = True
            return [
                AttemptStreamCommitted(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                )
            ]
        if isinstance(event, LLMStreamDiscardedEvent):
            return [
                AttemptStreamDiscarded(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                    reason=event.reason,
                )
            ]
        if isinstance(event, LLMStreamInterruptedEvent):
            return [
                AttemptStreamInterrupted(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                    reason=event.reason,
                )
            ]
        if isinstance(event, LLMStreamEndEvent):
            return []
        if isinstance(event, MessageAppendedEvent):
            return self._project_message(event)
        if isinstance(event, TurnContextCollectedEvent):
            summary = _summarize_reminder(event.content)
            return [SystemReminder(text=summary, lifetime=SystemReminderLifetime.TEMPORARY)] if summary else []
        return None

    def _project_message(self, event: MessageAppendedEvent) -> list[ViewEvent]:
        message = event.message
        role = message.role
        if str(role) != "assistant":
            self._streaming = False
            content = message.content
            if _is_system_reminder(content):
                summary = _summarize_reminder(content)
                if summary:
                    return [SystemReminder(text=summary, lifetime=SystemReminderLifetime.PERSISTENT)]
            return []
        content = message.content
        streamed = self._streaming
        self._streaming = False
        if not content.strip() and not streamed:
            return []
        return [
            MessageBlockCompleted(
                role="assistant",
                markdown=content,
                streamed=streamed,
            )
        ]


__all__ = ["MessageProjectionState"]
