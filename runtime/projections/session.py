"""Deterministic session projection shared by replay and live delivery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping, Optional

from mote.contracts.events import EventEnvelope, JsonValue, StreamId
from mote.contracts.handoff import PendingRuntimeHandoff, RuntimeHandoffResolution
from mote.contracts.models.failover import ModelCallSummary
from mote.contracts.models.routing import RoutingSessionState
from mote.contracts.ports.event_subscription import SubscriptionIdentity
from mote.contracts.runtimes import (
    RuntimeCheckpoint,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionAck,
    RuntimeProjectionRequest,
)
from mote.contracts.schema import Message
from mote.runtime.logging import log_class
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import (
    BrowserStateEvent,
    ContextCompactedFact,
    HistoryEditedFact,
    KernelStateEvent,
    LLMCallEvent,
    MessageEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    RoutingDecisionFact,
    RuntimeCheckpointEvent,
    RuntimeCommitEvent,
    RuntimeHandoffActivatedEvent,
    RuntimeHandoffPreparedEvent,
    RuntimeHandoffResolvedEvent,
    RuntimeOperationAbortedEvent,
    RuntimeOperationCompletedEvent,
    RuntimeOperationPreparedEvent,
    RuntimeProjectionAcknowledgedEvent,
    SessionEvent,
    SessionMetaEvent,
    TerminalStateEvent,
)

SESSION_PROJECTION_SUBSCRIPTION = SubscriptionIdentity("mote.session.projection")


class SessionProjectionSequenceError(ValueError):
    """A projection received an envelope outside its contiguous stream order."""


class ContextCompactionSourceError(ValueError):
    """A compaction fact does not cover the current model-context revision."""


@dataclass
class SessionProjectionState:
    """Current deterministic read model for one session stream."""

    transcript_messages: list[Message] = field(default_factory=list)
    model_context_messages: list[Message] = field(default_factory=list)
    meta: Optional[dict] = None
    message_events: int = 0
    compactions: int = 0
    history_edits: int = 0
    model_calls: dict[str, ModelCallSummary] = field(default_factory=dict)
    routing_state: RoutingSessionState = field(default_factory=RoutingSessionState)
    routing_decisions: dict[str, dict] = field(default_factory=dict)
    skipped: int = 0
    latest_compaction: Optional[ContextCompactedFact] = None
    terminal_state: Optional[dict] = None
    kernel_state: Optional[dict] = None
    browser_state: Optional[dict] = None
    runtime_checkpoints: dict[str, RuntimeCheckpoint] = field(default_factory=dict)
    pending_runtime_projections: dict[tuple[str, str], RuntimeProjectionRequest] = field(default_factory=dict)
    runtime_projection_dead_letters: dict[tuple[str, str], RuntimeProjectionAck] = field(default_factory=dict)
    pending_runtime_operations: dict[str, RuntimeOperationIntent] = field(default_factory=dict)
    completed_runtime_operations: dict[str, RuntimeOperationReceipt] = field(default_factory=dict)
    pending_runtime_handoffs: dict[str, PendingRuntimeHandoff] = field(default_factory=dict)
    runtime_handoff_resolutions: dict[str, RuntimeHandoffResolution] = field(default_factory=dict)
    output_state: Optional[dict] = None
    output_states: dict[str, dict] = field(default_factory=dict)
    through_sequence: int = 0
    acknowledged_runtime_projections: set[tuple[str, str]] = field(
        default_factory=set,
        repr=False,
    )


@log_class(
    level="DEBUG",
    exclude={"handle", "snapshot", "stream_id", "through_sequence"},
)
class SessionLiveProjection:
    """Session-scoped durable subscriber over the canonical session reducer.

    Startup rebuilds from the verified journal before the subscription worker
    consults its durable checkpoint. A checkpoint can therefore skip delivery
    only after this in-memory read model already contains the corresponding
    facts. Duplicate tail delivery is harmless because the reducer is sequence
    idempotent.
    """

    def __init__(self, stream_id: StreamId) -> None:
        self._stream_id = stream_id
        self._state = SessionProjectionState()

    @property
    def stream_id(self) -> StreamId:
        return self._stream_id

    @property
    def through_sequence(self) -> int:
        return self._state.through_sequence

    def restore(
        self,
        envelopes: Iterable[EventEnvelope[Mapping[str, JsonValue]]],
    ) -> None:
        """Atomically replace the read model with a full verified replay."""

        rebuilt = SessionProjectionState()
        for envelope in envelopes:
            self._validate_stream(envelope)
            reduce_session_envelope(rebuilt, envelope)
        self._state = rebuilt

    async def handle(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> None:
        """Apply one committed envelope from the owned subscription worker."""

        self._validate_stream(envelope)
        reduce_session_envelope(self._state, envelope)

    def snapshot(self) -> SessionProjectionState:
        """Return an isolated point-in-time view for readers."""

        return deepcopy(self._state)

    def _validate_stream(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> None:
        if envelope.stream_id != self._stream_id:
            raise ValueError("session projection received an envelope from another stream")


def reduce_session_envelope(
    state: SessionProjectionState,
    envelope: EventEnvelope[Mapping[str, JsonValue]],
) -> bool:
    """Apply one contiguous envelope exactly once; return whether it advanced."""

    if envelope.sequence <= state.through_sequence:
        return False
    expected = state.through_sequence + 1
    if envelope.sequence != expected:
        raise SessionProjectionSequenceError(
            f"session projection expected sequence {expected}, got {envelope.sequence}"
        )
    event = decode_session_event(envelope)
    if event is not None:
        reduce_session_event(state, event)
    state.through_sequence = envelope.sequence
    return True


def reduce_session_event(state: SessionProjectionState, event: SessionEvent) -> None:
    """Fold one current typed fact into ``state`` without IO or hidden tasks."""

    if isinstance(event, _OUTPUT_EVENTS) and event.run_id:
        state.output_state = dict(state.output_states.get(event.run_id, {}))

    if isinstance(event, SessionMetaEvent):
        state.meta = asdict(event)
    elif isinstance(event, MessageEvent):
        state.message_events += 1
        if event.message is None:
            state.skipped += 1
        else:
            state.transcript_messages.append(event.message)
            state.model_context_messages.append(event.message)
    elif isinstance(event, ContextCompactedFact):
        current_ids = [str(message.id) for message in state.model_context_messages]
        if event.source_message_ids != current_ids:
            raise ContextCompactionSourceError("context compaction source does not match the active model context")
        state.compactions += 1
        state.model_context_messages = list(event.model_context_messages)
        state.latest_compaction = event
    elif isinstance(event, HistoryEditedFact):
        state.history_edits += 1
        if event.clear_all:
            state.transcript_messages = []
            state.model_context_messages = []
        else:
            removed = frozenset(event.removed_message_ids)
            state.transcript_messages = [
                message for message in state.transcript_messages if str(message.id) not in removed
            ]
            state.model_context_messages = [
                message for message in state.model_context_messages if str(message.id) not in removed
            ]
    elif isinstance(event, LLMCallEvent) and event.summary is not None:
        state.model_calls[event.summary.model_call_id] = event.summary
    elif isinstance(event, RoutingDecisionFact):
        decision = event.decision
        decision_id = str(decision.get("decision_id", ""))
        if decision_id:
            state.routing_decisions[decision_id] = dict(decision)
        if event.state:
            state.routing_state = RoutingSessionState.model_validate(event.state)
    elif isinstance(event, TerminalStateEvent):
        state.terminal_state = {
            "cwd": event.cwd,
            "env": dict(event.env),
            "unset": list(event.unset),
        }
    elif isinstance(event, KernelStateEvent):
        state.kernel_state = {
            "cwd": event.cwd,
            "env": dict(event.env),
            "unset": list(event.unset),
        }
    elif isinstance(event, BrowserStateEvent):
        state.browser_state = {
            "urls": list(event.urls),
            "active": event.active,
            "storage_state": event.storage_state,
        }
    elif isinstance(event, RuntimeCheckpointEvent):
        checkpoint = event.checkpoint
        state.runtime_checkpoints[_runtime_key(checkpoint)] = checkpoint
    elif isinstance(event, RuntimeCommitEvent):
        checkpoint = event.fact.checkpoint
        state.runtime_checkpoints[_runtime_key(checkpoint)] = checkpoint
        for intent in event.fact.projections:
            request = RuntimeProjectionRequest(
                commit_id=event.fact.commit_id,
                checkpoint=checkpoint,
                intent=intent,
            )
            if request.key not in state.acknowledged_runtime_projections:
                state.pending_runtime_projections[request.key] = request
    elif isinstance(event, RuntimeProjectionAcknowledgedEvent):
        _reduce_runtime_projection_ack(state, event.ack)
    elif isinstance(event, RuntimeOperationPreparedEvent):
        state.pending_runtime_operations[event.intent.operation_id] = event.intent
    elif isinstance(event, RuntimeOperationCompletedEvent):
        intent = state.pending_runtime_operations.get(event.operation_id)
        receipt = event.receipt
        if receipt is None and intent is not None:
            receipt = RuntimeOperationReceipt.from_intent(intent)
        if receipt is not None:
            state.completed_runtime_operations[event.operation_id] = receipt
        state.pending_runtime_operations.pop(event.operation_id, None)
    elif isinstance(event, RuntimeOperationAbortedEvent):
        state.pending_runtime_operations.pop(event.operation_id, None)
    elif isinstance(event, RuntimeHandoffPreparedEvent):
        state.pending_runtime_handoffs[event.intent.handoff_id] = PendingRuntimeHandoff(intent=event.intent)
    elif isinstance(event, RuntimeHandoffActivatedEvent):
        pending = state.pending_runtime_handoffs.get(event.handoff_id)
        if pending is not None:
            state.pending_runtime_handoffs[event.handoff_id] = replace(
                pending,
                active=True,
            )
    elif isinstance(event, RuntimeHandoffResolvedEvent):
        resolution = event.resolution
        state.pending_runtime_handoffs.pop(resolution.handoff_id, None)
        state.runtime_handoff_resolutions[f"{resolution.kind}:{resolution.alias}"] = resolution
        if resolution.checkpoint is not None:
            state.runtime_checkpoints[_runtime_key(resolution.checkpoint)] = resolution.checkpoint
    elif isinstance(event, OutputCandidateReceivedEvent):
        state.output_state = {
            "status": "candidate_received",
            "candidate_id": event.candidate_id,
            "contract_id": event.contract_id,
            "schema_fingerprint": event.schema_fingerprint,
            "representation": event.representation,
            "raw": event.raw,
        }
        _set_run_id(state.output_state, event.run_id)
    elif isinstance(event, OutputValidationRejectedEvent):
        output = dict(state.output_state or {})
        output.update(
            status=("awaiting_correction" if event.correction_allowed else "correction_exhausted"),
            candidate_id=event.candidate_id,
            contract_id=event.contract_id,
            issues=list(event.issues),
            correction_attempts=event.correction_attempt,
            corrections_remaining=event.corrections_remaining,
        )
        if event.validator_provenance:
            output["validator_provenance"] = list(event.validator_provenance)
        _set_run_id(output, event.run_id)
        state.output_state = output
    elif isinstance(event, OutputAcceptedEvent):
        output = dict(state.output_state or {})
        output.update(
            {
                "status": "accepted",
                "candidate_id": event.candidate_id,
                "contract_id": event.contract_id,
                "schema_fingerprint": event.schema_fingerprint,
                "value": event.value,
                "correction_attempts": event.correction_attempts,
            }
        )
        if event.validator_provenance:
            output["validator_provenance"] = list(event.validator_provenance)
        _set_run_id(output, event.run_id)
        state.output_state = output
    elif isinstance(event, OutputCommitStartedEvent):
        output = dict(state.output_state or {})
        output.update(
            status="commit_started",
            candidate_id=event.candidate_id,
            contract_id=event.contract_id,
            fencing_token=event.fencing_token,
        )
        _set_run_id(output, event.run_id)
        state.output_state = output
    elif isinstance(event, OutputMigratedEvent):
        state.output_state = {
            "status": "accepted",
            "candidate_id": event.candidate_id,
            "contract_id": event.target_contract_id,
            "schema_fingerprint": event.target_schema_fingerprint,
            "value": event.value,
            "correction_attempts": 0,
            "migration_provenance": list(event.steps),
        }
        _set_run_id(state.output_state, event.run_id)
    elif isinstance(event, OutputCommittedEvent):
        prior_migration = (state.output_state or {}).get("migration_provenance")
        output = {
            "status": "committed",
            "candidate_id": event.candidate_id,
            "contract_id": event.contract_id,
            "schema_fingerprint": event.schema_fingerprint,
            "value": event.value,
            "correction_attempts": event.correction_attempts,
            "fencing_token": event.fencing_token,
        }
        if prior_migration:
            output["migration_provenance"] = prior_migration
        if event.validator_provenance:
            output["validator_provenance"] = list(event.validator_provenance)
        _set_run_id(output, event.run_id)
        state.output_state = output
    elif isinstance(event, OutputPublicationQueuedEvent):
        output = dict(state.output_state or {})
        output.update(
            status="publication_queued",
            publication_id=event.publication_id,
            candidate_id=event.candidate_id,
            contract_id=event.contract_id,
        )
        _set_run_id(output, event.run_id)
        state.output_state = output
    elif isinstance(event, OutputPublishedEvent):
        output = dict(state.output_state or {})
        output.update(
            status="published",
            candidate_id=event.candidate_id,
            contract_id=event.contract_id,
        )
        if event.publication_id:
            output["publication_id"] = event.publication_id
        _set_run_id(output, event.run_id)
        state.output_state = output

    if state.output_state and state.output_state.get("run_id"):
        state.output_state["run_kind"] = getattr(event, "run_kind", "agent")
        state.output_states[state.output_state["run_id"]] = dict(state.output_state)


def _reduce_runtime_projection_ack(
    state: SessionProjectionState,
    ack: RuntimeProjectionAck,
) -> None:
    if ack.status == "retry_scheduled":
        pending = state.pending_runtime_projections.get(ack.key)
        if pending is not None:
            state.pending_runtime_projections[ack.key] = replace(
                pending,
                attempts=max(pending.attempts, ack.attempts),
            )
        return
    state.acknowledged_runtime_projections.add(ack.key)
    state.pending_runtime_projections.pop(ack.key, None)
    if ack.status == "dead_letter":
        state.runtime_projection_dead_letters[ack.key] = ack


def _runtime_key(checkpoint: RuntimeCheckpoint) -> str:
    return f"{checkpoint.kind}:{checkpoint.alias}"


def _set_run_id(state: dict, run_id: str) -> None:
    if run_id:
        state["run_id"] = run_id


_OUTPUT_EVENTS = (
    OutputCandidateReceivedEvent,
    OutputValidationRejectedEvent,
    OutputAcceptedEvent,
    OutputCommitStartedEvent,
    OutputMigratedEvent,
    OutputCommittedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
)


__all__ = [
    "ContextCompactionSourceError",
    "SESSION_PROJECTION_SUBSCRIPTION",
    "SessionLiveProjection",
    "SessionProjectionSequenceError",
    "SessionProjectionState",
    "reduce_session_envelope",
    "reduce_session_event",
]
