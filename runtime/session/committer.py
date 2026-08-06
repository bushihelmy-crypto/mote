"""Explicit session-fact commit boundary over the process-local Event Fabric."""

from __future__ import annotations

from mote.contracts.events.conversation import (
    ContextCompactedEvent,
    HistoryEditedEvent,
    MessageAppendedEvent,
    PromptRejectedEvent,
)
from mote.contracts.events.model import InferenceCheckpointConsumedEvent, ModelCallFinishedEvent, RoutingDecisionEvent
from mote.contracts.events.output import (
    FinalOutputCommittedEvent,
    OutputCandidateReceivedEvent,
    OutputMigratedEvent,
    OutputValidationRejectedEvent,
)
from mote.contracts.events.pending_act import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
    ExternalEffectFinishedEvent,
    ExternalEffectInDoubtEvent,
    ExternalEffectStartedEvent,
    PendingActClaimAcquiredEvent,
    PendingActClaimReleasedEvent,
    PendingActClaimRenewedEvent,
    PendingActClaimTakenOverEvent,
    PendingActCreatedEvent,
    PendingActInterruptedEvent,
    PendingActionArgumentsRevisedEvent,
    PendingActionResultCommittedEvent,
    PendingActionsSkippedEvent,
    PendingActSchemaActivatedEvent,
    PendingActSettledEvent,
    RunRecoveryCursorAdvancedEvent,
    SessionPermissionRuleGrantedEvent,
    TurnInterruptedContextAttachedEvent,
    TurnInterruptedEvent,
    TurnInterruptSettledEvent,
)
from mote.contracts.events.session import TurnEndEvent
from mote.contracts.model.failover import ModelCallSummary
from mote.contracts.ports.events.journal import AppendResult
from mote.contracts.ports.session.facts import GuardedSessionFactBatch, RolloutSourceEvent
from mote.runtime.events.fabric import EventFabric
from mote.runtime.session.codec import encode_session_event
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

    async def commit_fact(self, event: RolloutSourceEvent) -> AppendResult:
        return await self.commit_facts((event,))

    async def commit_facts(self, events: tuple[RolloutSourceEvent, ...]) -> AppendResult:
        if not events:
            raise ValueError("session fact batch must not be empty")
        persisted_events = tuple(self._project_fact(event) for event in events)
        facts = tuple(encode_session_event(event, session_id=self._log.session_id) for event in persisted_events)
        result = await self._fabric.append(self._log.stream_id, facts)
        if any(isinstance(event, (PromptRejectedEvent, TurnEndEvent)) for event in events):
            await self._log.writer.drain()
        return result

    async def commit_guarded(self, batch: GuardedSessionFactBatch) -> AppendResult:
        persisted_events = tuple(self._project_fact(event) for event in batch.events)
        facts = tuple(encode_session_event(event, session_id=self._log.session_id) for event in persisted_events)
        return await self._fabric.append_guarded(
            self._log.stream_id,
            facts,
            expected_version=batch.expected_stream_version,
            writer=batch.writer,
        )

    @staticmethod
    def _project_fact(event: RolloutSourceEvent) -> SessionEvent:
        persisted: SessionEvent
        if isinstance(event, MessageAppendedEvent):
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
                route_schema_version=event.route_schema_version,
            )
        elif isinstance(
            event,
            (
                OutputCandidateReceivedEvent,
                OutputValidationRejectedEvent,
                OutputMigratedEvent,
                FinalOutputCommittedEvent,
                InferenceCheckpointConsumedEvent,
                PromptRejectedEvent,
                PendingActSchemaActivatedEvent,
                PendingActCreatedEvent,
                PendingActionArgumentsRevisedEvent,
                ApprovalRequestedEvent,
                ApprovalDecisionCommittedEvent,
                SessionPermissionRuleGrantedEvent,
                ExternalEffectStartedEvent,
                ExternalEffectFinishedEvent,
                ExternalEffectInDoubtEvent,
                PendingActSettledEvent,
                PendingActionResultCommittedEvent,
                PendingActionsSkippedEvent,
                PendingActClaimAcquiredEvent,
                PendingActClaimRenewedEvent,
                PendingActClaimTakenOverEvent,
                PendingActClaimReleasedEvent,
                RunRecoveryCursorAdvancedEvent,
                TurnInterruptedEvent,
                TurnInterruptedContextAttachedEvent,
                TurnInterruptSettledEvent,
                PendingActInterruptedEvent,
            ),
        ):
            persisted = event
        else:
            raise TypeError(f"event has no session fact projection: {type(event).__name__}")
        return persisted


__all__ = ["SessionFactCommitter"]
