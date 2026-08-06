"""Deterministic session projection shared by replay and live delivery."""

from __future__ import annotations

import hashlib
from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping, Optional

from mote.contracts.conversation import Message
from mote.contracts.conversation.fields import TOOL_EFFECT_PRESENTATION_DIGEST, TOOL_EFFECT_RECEIPT_ID
from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId
from mote.contracts.execution.interrupt_context import TURN_ABORTED_FRAGMENT
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingActionArgumentsRevision,
)
from mote.contracts.execution.pending_act_claim import PendingActExecutionClaim
from mote.contracts.execution.run_cursor import RunRecoveryCursor
from mote.contracts.interaction.approval import ApprovalDisposition, ApprovalRequest, ApprovalState
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.model.failover import ModelCallSummary
from mote.contracts.model.routing import RoutingSessionState
from mote.contracts.ports.events.subscription import SubscriptionIdentity
from mote.contracts.runtime import (
    RuntimeCheckpoint,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionAck,
    RuntimeProjectionRequest,
    validate_checkpoint_successor,
)
from mote.contracts.runtime.handoff import PendingRuntimeHandoff, RuntimeHandoffResolution
from mote.contracts.tool.external_effect import ExternalEffectState, ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationId, ToolInvocationIdentity
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
    ContextCompactedFact,
    ExternalEffectFinishedEvent,
    ExternalEffectInDoubtEvent,
    ExternalEffectStartedEvent,
    FinalOutputCommittedEvent,
    HistoryEditedFact,
    InferenceCheckpointConsumedEvent,
    LLMCallEvent,
    MessageEvent,
    OutputCandidateReceivedEvent,
    OutputMigratedEvent,
    OutputValidationRejectedEvent,
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
    RoutingDecisionFact,
    RunRecoveryCursorAdvancedEvent,
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
    SessionPermissionRuleGrantedEvent,
    TurnInterruptedContextAttachedEvent,
    TurnInterruptedEvent,
    TurnInterruptSettledEvent,
)
from mote.runtime.telemetry.logging import log_class

SESSION_PROJECTION_SUBSCRIPTION = SubscriptionIdentity("mote.session.projection")


class SessionProjectionSequenceError(ValueError):
    """A projection received an envelope outside its contiguous stream order."""


class ContextCompactionSourceError(ValueError):
    """A compaction fact does not cover the current model-context revision."""


class SessionProjectionIdentityError(ValueError):
    """A Session stream violated its unique metadata identity invariant."""


@dataclass(frozen=True, slots=True)
class ApprovalProjection:
    request: ApprovalRequest
    state: ApprovalState
    disposition: ApprovalDisposition | None = None


@dataclass(frozen=True, slots=True)
class ExternalEffectProjection:
    state: ExternalEffectState
    receipt: ToolEffectReceipt | None = None
    evidence: JsonValue = None


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
    consumed_inference_checkpoints: dict[str, InferenceCheckpointConsumedEvent] = field(default_factory=dict)
    routing_state: RoutingSessionState = field(default_factory=RoutingSessionState)
    routing_decisions: dict[str, dict] = field(default_factory=dict)
    latest_compaction: Optional[ContextCompactedFact] = None
    runtime_checkpoints: dict[str, RuntimeCheckpoint] = field(default_factory=dict)
    pending_runtime_projections: dict[tuple[str, str], RuntimeProjectionRequest] = field(default_factory=dict)
    runtime_projection_dead_letters: dict[tuple[str, str], RuntimeProjectionAck] = field(default_factory=dict)
    pending_runtime_operations: dict[str, RuntimeOperationIntent] = field(default_factory=dict)
    completed_runtime_operations: dict[str, RuntimeOperationReceipt] = field(default_factory=dict)
    pending_runtime_handoffs: dict[str, PendingRuntimeHandoff] = field(default_factory=dict)
    runtime_handoff_resolutions: dict[str, RuntimeHandoffResolution] = field(default_factory=dict)
    output_state: Optional[dict] = None
    output_states: dict[str, dict] = field(default_factory=dict)
    pending_act_schema_activated: bool = False
    pending_act_schema_activated_runs: set[str] = field(default_factory=set)
    pending_act_by_id: dict[PendingActFrontierId, PendingActFrontier] = field(default_factory=dict)
    active_pending_act_by_run: dict[str, PendingActFrontierId] = field(default_factory=dict)
    pending_action_arguments_by_invocation: dict[ToolInvocationId, tuple[PendingActionArgumentsRevision, ...]] = field(
        default_factory=dict
    )
    approval_by_request_id: dict[ApprovalRequestId, ApprovalProjection] = field(default_factory=dict)
    session_permission_rules: tuple[SessionPermissionRuleGrantedEvent, ...] = ()
    external_effect_by_invocation: dict[ToolInvocationId, ExternalEffectProjection] = field(default_factory=dict)
    external_effect_identity_by_invocation: dict[ToolInvocationId, ToolInvocationIdentity] = field(default_factory=dict)
    run_cursor_by_run_id: dict[str, RunRecoveryCursor] = field(default_factory=dict)
    claim_by_frontier_id: dict[PendingActFrontierId, PendingActExecutionClaim] = field(default_factory=dict)
    pending_action_result_by_invocation: dict[ToolInvocationId, PendingActionResultCommittedEvent] = field(
        default_factory=dict
    )
    skipped_pending_actions: set[ToolInvocationId] = field(default_factory=set)
    pending_result_messages_by_id: dict[str, Message] = field(default_factory=dict)
    interrupted_run_by_id: dict[str, TurnInterruptedEvent] = field(default_factory=dict)
    interrupt_context_by_run: dict[str, TurnInterruptedContextAttachedEvent] = field(default_factory=dict)
    settled_interrupt_runs: set[str] = field(default_factory=set)
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

        state = copy(self._state)
        state.transcript_messages = deepcopy(self._state.transcript_messages)
        state.model_context_messages = deepcopy(self._state.model_context_messages)
        state.meta = deepcopy(self._state.meta)
        state.model_calls = deepcopy(self._state.model_calls)
        state.consumed_inference_checkpoints = dict(self._state.consumed_inference_checkpoints)
        state.routing_state = deepcopy(self._state.routing_state)
        state.routing_decisions = deepcopy(self._state.routing_decisions)
        state.latest_compaction = deepcopy(self._state.latest_compaction)
        state.runtime_checkpoints = dict(self._state.runtime_checkpoints)
        state.pending_runtime_projections = dict(self._state.pending_runtime_projections)
        state.runtime_projection_dead_letters = dict(self._state.runtime_projection_dead_letters)
        state.pending_runtime_operations = dict(self._state.pending_runtime_operations)
        state.completed_runtime_operations = dict(self._state.completed_runtime_operations)
        state.pending_runtime_handoffs = dict(self._state.pending_runtime_handoffs)
        state.runtime_handoff_resolutions = dict(self._state.runtime_handoff_resolutions)
        state.output_state = deepcopy(self._state.output_state)
        state.output_states = deepcopy(self._state.output_states)
        state.pending_act_schema_activated = self._state.pending_act_schema_activated
        state.pending_act_by_id = dict(self._state.pending_act_by_id)
        state.pending_act_schema_activated_runs = set(self._state.pending_act_schema_activated_runs)
        state.active_pending_act_by_run = dict(self._state.active_pending_act_by_run)
        state.pending_action_arguments_by_invocation = dict(self._state.pending_action_arguments_by_invocation)
        state.approval_by_request_id = dict(self._state.approval_by_request_id)
        state.external_effect_by_invocation = dict(self._state.external_effect_by_invocation)
        state.external_effect_identity_by_invocation = dict(self._state.external_effect_identity_by_invocation)
        state.run_cursor_by_run_id = dict(self._state.run_cursor_by_run_id)
        state.claim_by_frontier_id = dict(self._state.claim_by_frontier_id)
        state.acknowledged_runtime_projections = set(self._state.acknowledged_runtime_projections)
        return state

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
    if isinstance(event, SessionMetaEvent):
        if state.meta is not None or envelope.sequence != 1:
            raise SessionProjectionIdentityError("Session metadata must be the unique first fact")
        if envelope.session_id != event.session_id or str(envelope.stream_id) != f"session/{event.session_id}":
            raise SessionProjectionIdentityError("Session metadata, envelope, and stream identities differ")
    elif state.meta is None:
        raise SessionProjectionIdentityError("Session projection cannot advance before metadata")
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
    elif isinstance(event, InferenceCheckpointConsumedEvent):
        prior = state.consumed_inference_checkpoints.get(event.operation_id)
        if prior is not None and prior != event:
            raise ValueError("inference checkpoint operation identity forked")
        state.consumed_inference_checkpoints[event.operation_id] = event
    elif isinstance(event, RoutingDecisionFact):
        decision = event.decision
        decision_id = str(decision.get("decision_id", ""))
        if decision_id:
            state.routing_decisions[decision_id] = dict(decision)
        if event.state:
            state.routing_state = RoutingSessionState.model_validate(event.state)
    elif isinstance(event, PendingActSchemaActivatedEvent):
        state.pending_act_schema_activated = True
        state.pending_act_schema_activated_runs.add(event.activated_run_id)
    elif isinstance(event, PendingActCreatedEvent):
        frontier = event.frontier
        if frontier.frontier_id in state.pending_act_by_id:
            raise ValueError("PendingAct frontier identity was reused")
        if frontier.run_id in state.active_pending_act_by_run:
            raise ValueError("run already has an active PendingAct frontier")
        state.pending_act_by_id[frontier.frontier_id] = frontier
        state.active_pending_act_by_run[frontier.run_id] = frontier.frontier_id
    elif isinstance(event, PendingActionArgumentsRevisedEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        if frontier is None:
            raise ValueError("argument revision references an unknown PendingAct")
        if event.revision.invocation_id not in {action.invocation_id for action in frontier.actions}:
            raise ValueError("argument revision references an unknown invocation")
        prior = state.pending_action_arguments_by_invocation.get(event.revision.invocation_id, ())
        if event.revision.revision != len(prior):
            raise ValueError("argument revisions must be contiguous from zero")
        if prior and event.previous_arguments_digest != prior[-1].arguments_digest:
            raise ValueError("argument revision previous digest does not match")
        if not prior and event.previous_arguments_digest is not None:
            raise ValueError("initial argument revision cannot have a previous digest")
        state.pending_action_arguments_by_invocation[event.revision.invocation_id] = prior + (event.revision,)
    elif isinstance(event, ApprovalRequestedEvent):
        request = event.request
        assert request.request_id is not None and request.frontier_id is not None and request.invocation_id is not None
        if request.request_id in state.approval_by_request_id:
            raise ValueError("approval request identity was reused")
        frontier = state.pending_act_by_id.get(request.frontier_id)
        if frontier is None or request.invocation_id not in {action.invocation_id for action in frontier.actions}:
            raise ValueError("approval request references an unknown PendingAct action")
        revisions = state.pending_action_arguments_by_invocation.get(request.invocation_id, ())
        if (
            not revisions
            or revisions[-1].revision != request.arguments_revision
            or revisions[-1].arguments_digest != request.arguments_digest
        ):
            raise ValueError("approval request does not bind the current arguments revision")
        state.approval_by_request_id[request.request_id] = ApprovalProjection(request, ApprovalState.WAITING)
    elif isinstance(event, ApprovalDecisionCommittedEvent):
        approval = state.approval_by_request_id.get(event.request_id)
        if approval is None or approval.state is not ApprovalState.WAITING:
            raise ValueError("approval decision requires one waiting request")
        if (
            approval.request.arguments_revision != event.arguments_revision
            or approval.request.arguments_digest != event.arguments_digest
        ):
            raise ValueError("approval decision arguments do not match its request")
        state.approval_by_request_id[event.request_id] = ApprovalProjection(
            approval.request, event.state, event.disposition
        )
    elif isinstance(event, SessionPermissionRuleGrantedEvent):
        approval = state.approval_by_request_id.get(event.request_id)
        if approval is None or approval.disposition is not ApprovalDisposition.ALLOW_SESSION:
            raise ValueError("session permission rule requires allow-session approval")
        state.session_permission_rules += (event,)
    elif isinstance(event, ExternalEffectStartedEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        if frontier is None:
            raise ValueError("external effect references an unknown PendingAct")
        action = next(
            (item for item in frontier.actions if item.invocation_id == event.identity.invocation_id),
            None,
        )
        if action is None or action.effect.value != "external":
            raise ValueError("external effect requires an EXTERNAL PendingAct action")
        if event.approval_request_id is not None:
            approval = state.approval_by_request_id.get(event.approval_request_id)
            if approval is None or approval.state is not ApprovalState.APPROVED:
                raise ValueError("external effect approval gate is not satisfied")
        if event.identity.invocation_id in state.external_effect_by_invocation:
            raise ValueError("external effect was already started")
        state.external_effect_by_invocation[event.identity.invocation_id] = ExternalEffectProjection(
            ExternalEffectState.STARTED
        )
        state.external_effect_identity_by_invocation[event.identity.invocation_id] = event.identity
    elif isinstance(event, ExternalEffectFinishedEvent):
        effect = state.external_effect_by_invocation.get(event.invocation_id)
        if effect is None or effect.state is not ExternalEffectState.STARTED:
            raise ValueError("external effect terminal requires STARTED")
        state.external_effect_by_invocation[event.invocation_id] = ExternalEffectProjection(
            event.disposition, event.receipt
        )
    elif isinstance(event, ExternalEffectInDoubtEvent):
        effect = state.external_effect_by_invocation.get(event.invocation_id)
        if effect is None or effect.state is not ExternalEffectState.STARTED:
            raise ValueError("external effect IN_DOUBT requires STARTED")
        state.external_effect_by_invocation[event.invocation_id] = ExternalEffectProjection(
            ExternalEffectState.IN_DOUBT, evidence=event.evidence
        )
    elif isinstance(event, PendingActSettledEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        if frontier is None or frontier.revision != event.final_revision:
            raise ValueError("PendingAct settlement revision mismatch")
        if state.active_pending_act_by_run.get(frontier.run_id) != event.frontier_id:
            raise ValueError("PendingAct settlement does not own the active run frontier")
        accounted = set(state.pending_action_result_by_invocation) | state.skipped_pending_actions
        if {action.invocation_id for action in frontier.actions} - accounted:
            raise ValueError("PendingAct settlement has unaccounted actions")
        state.active_pending_act_by_run.pop(frontier.run_id)
    elif isinstance(event, TurnInterruptedEvent):
        if event.run_id in state.interrupted_run_by_id:
            raise ValueError("run was interrupted more than once")
        state.interrupted_run_by_id[event.run_id] = event
    elif isinstance(event, PendingActInterruptedEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        if frontier is None or frontier.revision != event.final_revision:
            raise ValueError("interrupted PendingAct revision mismatch")
        if frontier.run_id not in state.interrupted_run_by_id:
            raise ValueError("PendingAct interrupt requires a durable turn interrupt")
        state.active_pending_act_by_run.pop(frontier.run_id, None)
    elif isinstance(event, TurnInterruptedContextAttachedEvent):
        if event.run_id not in state.interrupted_run_by_id or event.run_id in state.interrupt_context_by_run:
            raise ValueError("interrupt context attachment is invalid")
        if not any(message.id == event.anchor_message_id for message in state.transcript_messages):
            raise ValueError("interrupt context anchor message is unknown")
        state.interrupt_context_by_run[event.run_id] = event
        state.model_context_messages = [
            (
                message.model_copy(
                    update={"content": _attach_interrupt_fragment(message.content)},
                    deep=True,
                )
                if message.id == event.anchor_message_id
                else message
            )
            for message in state.model_context_messages
        ]
    elif isinstance(event, TurnInterruptSettledEvent):
        if event.run_id not in state.interrupted_run_by_id:
            raise ValueError("interrupt settlement requires a durable interrupt")
        if event.run_id not in state.interrupt_context_by_run:
            raise ValueError("interrupt settlement requires a context attachment")
        if event.run_id in state.settled_interrupt_runs:
            raise ValueError("turn interrupt was settled more than once")
        state.settled_interrupt_runs.add(event.run_id)
    elif isinstance(event, PendingActionResultCommittedEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        action = (
            None
            if frontier is None
            else next(
                (item for item in frontier.actions if item.invocation_id == event.invocation_id),
                None,
            )
        )
        if action is None:
            raise ValueError("result references an unknown PendingAct action")
        if event.invocation_id in state.pending_action_result_by_invocation:
            raise ValueError("PendingAct action result was committed more than once")
        if event.invocation_id in state.skipped_pending_actions:
            raise ValueError("skipped PendingAct action cannot receive a result")
        effect = state.external_effect_by_invocation.get(event.invocation_id)
        if action.effect.value == "external":
            if effect is None or effect.state not in {
                ExternalEffectState.SUCCEEDED,
                ExternalEffectState.FAILED,
                ExternalEffectState.IN_DOUBT,
            }:
                raise ValueError("external result requires a terminal effect fact")
            if effect.state is ExternalEffectState.IN_DOUBT:
                if event.receipt_id is not None or event.presentation_digest is not None:
                    raise ValueError("in-doubt external result cannot claim a receipt")
            elif (
                effect.receipt is None
                or event.receipt_id != effect.receipt.receipt_id
                or event.presentation_digest != effect.receipt.presentation_digest
            ):
                raise ValueError("external result receipt identity or digest does not match")
        elif event.receipt_id is not None or event.presentation_digest is not None:
            raise ValueError("non-external result cannot reference an external receipt")
        message = next(
            (item for item in reversed(state.transcript_messages) if item.id == event.message_id),
            None,
        )
        if message is None:
            raise ValueError("PendingAct result references an unknown message")
        if action.effect.value == "external" and effect is not None and effect.receipt is not None:
            if message.metadata.get(TOOL_EFFECT_RECEIPT_ID) != event.receipt_id:
                raise ValueError("ToolResult message receipt identity does not match")
            if message.metadata.get(TOOL_EFFECT_PRESENTATION_DIGEST) != event.presentation_digest:
                raise ValueError("ToolResult message presentation digest does not match")
            content_digest = f"sha256-{hashlib.sha256(message.content.encode('utf-8')).hexdigest()}"
            if content_digest != event.presentation_digest:
                raise ValueError("ToolResult message content digest does not match receipt")
        state.pending_action_result_by_invocation[event.invocation_id] = event
        state.pending_result_messages_by_id[event.message_id] = message
    elif isinstance(event, PendingActionsSkippedEvent):
        frontier = state.pending_act_by_id.get(event.frontier_id)
        if frontier is None:
            raise ValueError("skipped actions reference an unknown PendingAct")
        action_ids = {item.invocation_id for item in frontier.actions}
        if any(item not in action_ids for item in event.invocation_ids):
            raise ValueError("skipped actions contain an unknown invocation")
        if any(item in state.pending_action_result_by_invocation for item in event.invocation_ids):
            raise ValueError("completed PendingAct action cannot be skipped")
        if any(item in state.skipped_pending_actions for item in event.invocation_ids):
            raise ValueError("PendingAct action was skipped more than once")
        state.skipped_pending_actions.update(event.invocation_ids)
    elif isinstance(event, PendingActClaimRenewedEvent):
        claim = event.claim
        prior = state.claim_by_frontier_id.get(claim.frontier_id)
        if (
            prior is None
            or claim.claim_id != prior.claim_id
            or claim.owner_id != prior.owner_id
            or claim.incarnation_id != prior.incarnation_id
        ):
            raise ValueError("claim renew owner mismatch")
        if claim.claim_revision != prior.claim_revision + 1 or claim.fencing_token != prior.fencing_token:
            raise ValueError("claim renew revision or fence is invalid")
        state.claim_by_frontier_id[claim.frontier_id] = claim
    elif isinstance(event, PendingActClaimTakenOverEvent):
        claim = event.claim
        prior = state.claim_by_frontier_id.get(claim.frontier_id)
        if (
            prior is None
            or claim.claim_revision != prior.claim_revision + 1
            or claim.fencing_token != prior.fencing_token + 1
        ):
            raise ValueError("claim takeover revision or fence is invalid")
        state.claim_by_frontier_id[claim.frontier_id] = claim
    elif isinstance(event, PendingActClaimAcquiredEvent):
        claim = event.claim
        if claim.frontier_id not in state.pending_act_by_id or claim.frontier_id in state.claim_by_frontier_id:
            raise ValueError("claim acquire requires an unclaimed PendingAct")
        state.claim_by_frontier_id[claim.frontier_id] = claim
    elif isinstance(event, PendingActClaimReleasedEvent):
        prior = state.claim_by_frontier_id.get(event.frontier_id)
        if (
            prior is None
            or event.claim_id != prior.claim_id
            or event.claim_revision != prior.claim_revision + 1
            or event.fencing_token != prior.fencing_token
        ):
            raise ValueError("claim release identity is invalid")
        state.claim_by_frontier_id.pop(event.frontier_id)
    elif isinstance(event, RunRecoveryCursorAdvancedEvent):
        cursor = event.cursor
        prior = state.run_cursor_by_run_id.get(cursor.run_id)
        if prior is not None and cursor.revision != prior.revision + 1:
            raise ValueError("run cursor revision must advance by one")
        if prior is None and cursor.revision != 0:
            raise ValueError("initial run cursor revision must be zero")
        if cursor.pending_act_id is not None and state.pending_act_by_id.get(cursor.pending_act_id) is None:
            raise ValueError("run cursor references an unknown PendingAct")
        state.run_cursor_by_run_id[cursor.run_id] = cursor
    elif isinstance(event, RuntimeCheckpointEvent):
        checkpoint = event.checkpoint
        _advance_runtime_checkpoint(state, checkpoint)
    elif isinstance(event, RuntimeCommitEvent):
        checkpoint = event.fact.checkpoint
        _advance_runtime_checkpoint(state, checkpoint)
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
            _advance_runtime_checkpoint(state, resolution.checkpoint)
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
    elif isinstance(event, FinalOutputCommittedEvent):
        assert event.message is not None
        state.message_events += 1
        state.transcript_messages.append(event.message)
        state.model_context_messages.append(event.message)
        output = {
            "status": "committed",
            "candidate_id": event.candidate_id,
            "contract_id": event.contract_id,
            "schema_fingerprint": event.schema_fingerprint,
            "value": event.value,
            "correction_attempts": event.correction_attempts,
            "fencing_token": event.fencing_token,
            "message": event.message,
        }
        if event.validator_provenance:
            output["validator_provenance"] = list(event.validator_provenance)
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


def _advance_runtime_checkpoint(state: SessionProjectionState, checkpoint: RuntimeCheckpoint) -> None:
    key = _runtime_key(checkpoint)
    validate_checkpoint_successor(state.runtime_checkpoints.get(key), checkpoint)
    state.runtime_checkpoints[key] = checkpoint


def _set_run_id(state: dict, run_id: str) -> None:
    if run_id:
        state["run_id"] = run_id


def _attach_interrupt_fragment(content: str) -> str:
    if TURN_ABORTED_FRAGMENT in content:
        return content
    return f"{content.rstrip()}\n\n{TURN_ABORTED_FRAGMENT}" if content else TURN_ABORTED_FRAGMENT


_OUTPUT_EVENTS = (
    FinalOutputCommittedEvent,
    OutputCandidateReceivedEvent,
    OutputValidationRejectedEvent,
    OutputMigratedEvent,
)


__all__ = [
    "ContextCompactionSourceError",
    "SESSION_PROJECTION_SUBSCRIPTION",
    "SessionLiveProjection",
    "SessionProjectionIdentityError",
    "SessionProjectionSequenceError",
    "SessionProjectionState",
    "reduce_session_envelope",
    "reduce_session_event",
]
