"""Budget and context-lifecycle event projections."""

from __future__ import annotations

from typing import Optional

from mote.contracts.events.agent import BudgetEvent
from mote.contracts.events.conversation import ContextCompactedEvent
from mote.contracts.events.model import ModelAttemptFinishedEvent
from mote.product.presentation.events.events import ConversationCompacted, Notice, ViewEvent
from mote.product.presentation.input_events import PresentationInputEvent


def project_system_event(event: PresentationInputEvent) -> Optional[list[ViewEvent]]:
    if isinstance(event, BudgetEvent):
        spend = event.spend
        limit = event.limit
        if event.stopped:
            text = f"Budget cap reached (${spend:.2f} / ${limit:.2f}). " "Stopping — no further model calls."
        else:
            percentage = int(event.fraction * 100)
            text = f"Budget warning: {percentage}% of cap used " f"(${spend:.2f} / ${limit:.2f})."
        return [Notice(text=text, level="warning")]
    if isinstance(event, ContextCompactedEvent):
        return [
            ConversationCompacted(
                summary=event.summary,
                message_count=len(event.model_context_messages),
            )
        ]
    if isinstance(event, ModelAttemptFinishedEvent):
        return []
    return None


__all__ = ["project_system_event"]
