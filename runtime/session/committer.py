"""Explicit session-fact commit boundary over the process-local Event Fabric."""

from __future__ import annotations

from typing import cast

from mote.contracts.events.types import (
    ContextCompactedEvent,
    HistoryEditedEvent,
    MessageAppendedEvent,
    ModelCallFinishedEvent,
    PromptRejectedEvent,
    RoutingDecisionEvent,
    TurnEndEvent,
)
from mote.contracts.models.failover import ModelCallSummary
from mote.contracts.ports.event_journal import AppendResult
from mote.runtime.events.fabric import EventFabric
from mote.runtime.session.codec import encode_session_event
from mote.runtime.session.event_policy import is_rollout_event
from mote.runtime.session.events import (
    ContextCompactedFact,
    HistoryEditedFact,
    LLMCallEvent,
    MessageEvent,
    RoutingDecisionFact,
    SessionEvent,
    TurnContextEvent,
)
from mote.runtime.session.log import SessionLog


class SessionFactCommitter:
    """Translate session-owned facts and commit them through one fabric owner."""

    def __init__(self, log: SessionLog, fabric: EventFabric) -> None:
        self._log = log
        self._fabric = fabric

    async def commit_event(self, event: SessionEvent) -> AppendResult:
        fact = encode_session_event(event, session_id=self._log.session_id)
        return await self._fabric.append(self._log.stream_id, (fact,))

    def commit_event_from_thread(self, event: SessionEvent) -> AppendResult:
        """Commit a synchronous transaction fact through the fabric owner loop."""

        fact = encode_session_event(event, session_id=self._log.session_id)
        return self._fabric.append_from_thread(self._log.stream_id, (fact,))

    async def commit_fact(self, event: object) -> AppendResult | None:
        if not is_rollout_event(event):
            raise TypeError(f"event is not a session fact: {type(event).__name__}")
        persisted: SessionEvent
        if isinstance(event, MessageAppendedEvent):
            if event.message is None:
                return None
            persisted = MessageEvent(message=event.message)
        elif isinstance(event, ModelCallFinishedEvent):
            persisted = LLMCallEvent(
                request_id=event.model_call_id,
                model=event.selected_endpoint_id or None,
                usage=event.usage,
                cost_usd=event.cost_usd,
                summary=ModelCallSummary.model_validate(event.summary),
            )
        elif isinstance(event, ContextCompactedEvent):
            persisted = ContextCompactedFact(
                model_context_messages=list(event.model_context_messages),
                source_message_ids=list(event.source_message_ids),
                summary=event.summary or "",
                strategy=event.strategy,
                trigger=event.trigger,
            )
        elif isinstance(event, HistoryEditedEvent):
            persisted = HistoryEditedFact(
                removed_message_ids=list(event.removed_message_ids),
                clear_all=event.clear_all,
                reason=event.reason,
            )
        elif isinstance(event, TurnEndEvent):
            persisted = TurnContextEvent(
                turn_id=event.turn_id,
                working_dir=event.working_dir,
                model=event.model,
                token_state=event.token_state,
            )
        elif isinstance(event, RoutingDecisionEvent):
            persisted = RoutingDecisionFact(
                decision=dict(event.decision),
                state=dict(event.state),
            )
        else:
            persisted = cast(SessionEvent, event)
        result = await self.commit_event(persisted)
        if isinstance(event, (PromptRejectedEvent, TurnEndEvent)):
            await self._log.writer.drain()
        return result


__all__ = ["SessionFactCommitter"]
