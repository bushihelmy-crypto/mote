"""Runtime adapter that atomically accepts a model-emitted Act frontier."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from mote.contracts.conversation import Message
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.contracts.events.envelope import JsonValue
from mote.contracts.events.pending_act import PendingActionResultCommittedEvent, PendingActionsSkippedEvent
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.pending_act_claim import PendingActExecutionClaim, PendingActInvokePermit
from mote.contracts.execution.pending_act_identity import PendingActFrontierId
from mote.contracts.interaction.approval import ApprovalChoice, ApprovalDisposition, ApprovalRequest, ApprovalState
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.execution.checkpoint import InferenceCheckpointPort
from mote.contracts.ports.execution.pending_act import ExternalEffectPermit, PendingActAcceptance, PendingActResume
from mote.contracts.ports.tool.approval import ToolApprovalIntent, ToolApprovalResolution
from mote.contracts.tool.actions import ToolCallAction
from mote.contracts.tool.catalog import ToolBindingSnapshot
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.external_effect import ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationId, tool_arguments_digest
from mote.runtime.session.durable_approval import DurableApprovalCoordinator
from mote.runtime.session.pending_act import RuntimePendingActService
from mote.runtime.session.pending_act_claim import PendingActClaimService
from mote.runtime.session.projection import SessionLiveProjection, SessionProjectionState
from mote.runtime.session.writer_guard import SessionRunWriterGuard


class RuntimePendingActAcceptance:
    def __init__(
        self,
        service: RuntimePendingActService,
        projection: SessionLiveProjection,
        checkpoint: InferenceCheckpointPort,
        writer_guard: SessionRunWriterGuard,
        claim_service: PendingActClaimService,
        approval: DurableApprovalCoordinator,
        *,
        session_id: str,
        run_id: str,
        fencing_token: int,
        request_approval,
    ) -> None:
        self._service = service
        self._projection = projection
        self._checkpoint = checkpoint
        self._writer_guard = writer_guard
        self._claim_service = claim_service
        self._approval = approval
        self._request_approval = request_approval
        self._session_id = session_id
        self._run_id = run_id
        self._fencing_token = fencing_token
        self._claim: PendingActExecutionClaim | None = None

    async def accept(
        self,
        actions: tuple[ToolCallAction, ...],
        snapshot: ToolBindingSnapshot,
        messages: tuple[Message, ...],
    ) -> PendingActAcceptance:
        if not actions:
            raise ValueError("PendingAct acceptance requires at least one tool call")
        definitions = {definition.name: definition for definition in snapshot.catalog.definitions}
        frontier_id = PendingActFrontierId(uuid4().hex)
        pending_actions: list[PendingAction] = []
        revisions: list[PendingActionArgumentsRevision] = []
        for ordinal, action in enumerate(actions):
            definition = definitions.get(action.name)
            if definition is None:
                raise ValueError(f"tool definition is absent from pinned snapshot: {action.name}")
            invocation_id = ToolInvocationId(action.action_id or uuid4().hex)
            digest = tool_arguments_digest(action.arguments)
            pending_actions.append(
                PendingAction(
                    ordinal,
                    invocation_id,
                    action.action_id or invocation_id.value,
                    action.name,
                    definition.semantic_identity,
                    snapshot.registry_revision,
                    ToolEffect(definition.effect),
                    0,
                    (
                        _fileops_transaction_id(frontier_id, invocation_id)
                        if ToolEffect(definition.effect) is ToolEffect.LOCAL
                        else None
                    ),
                )
            )
            revisions.append(PendingActionArgumentsRevision(invocation_id, 0, action.arguments, digest))
        checkpoint_event = await self._checkpoint.prepare_consumption(f"pending-act:{frontier_id.value}:accept")
        frontier = PendingActFrontier(
            1,
            frontier_id,
            self._session_id,
            self._run_id,
            checkpoint_event.model_call_id,
            0,
            _definition_ref(snapshot),
            tuple(pending_actions),
        )
        expected_version = self._projection.snapshot().through_sequence
        writer = self._writer_guard.writer_for(self._run_id, self._fencing_token)
        await self._service.create(
            frontier,
            tuple(revisions),
            expected_stream_version=expected_version,
            writer=writer,
            leading_events=tuple(MessageAppendedEvent(message) for message in messages) + (checkpoint_event,),
        )
        self._checkpoint.acknowledge_consumption(checkpoint_event)
        await self._acquire_claim(frontier)
        return PendingActAcceptance(frontier)

    async def settle(
        self,
        acceptance: PendingActAcceptance,
        messages: tuple[Message, ...],
        *,
        continue_inference: bool,
        effect_receipts: tuple[ToolEffectReceipt, ...] = (),
        action_results: tuple[PendingActionResultCommittedEvent, ...] = (),
        skipped: PendingActionsSkippedEvent | None = None,
        rejected_approval_request_id: ApprovalRequestId | None = None,
    ) -> None:
        frontier = acceptance.frontier
        expected_version = self._projection.snapshot().through_sequence
        writer = self._writer_guard.writer_for(self._run_id, self._fencing_token)
        await self._service.settle(
            frontier.frontier_id,
            tuple(MessageAppendedEvent(message) for message in messages),
            expected_frontier_revision=frontier.revision,
            continue_inference=continue_inference,
            expected_stream_version=expected_version,
            writer=writer,
            effect_receipts=effect_receipts,
            action_results=action_results,
            skipped=skipped,
            rejected_approval_request_id=rejected_approval_request_id,
        )
        await self._release_claim()

    def resume(self, frontier: PendingActFrontier, snapshot: ToolBindingSnapshot) -> PendingActResume:
        if frontier.run_id != self._run_id or frontier.session_id != self._session_id:
            raise ValueError("recovered PendingAct belongs to another run or Session")
        state = self._projection.snapshot()
        if state.active_pending_act_by_run.get(self._run_id) != frontier.frontier_id:
            raise ValueError("recovered PendingAct is not the active run frontier")
        claim = state.claim_by_frontier_id.get(frontier.frontier_id)
        if claim is not None:
            if (
                claim.owner_id != self._writer_guard.owner_id
                or claim.incarnation_id != self._writer_guard.incarnation_id
            ):
                raise ValueError("recovered PendingAct is claimed by another incarnation")
            self._claim = claim
        definition = frontier.definition_ref
        if (
            snapshot.composition_generation_id != definition.composition_generation_id
            or snapshot.catalog.fingerprint != definition.catalog_fingerprint
            or snapshot.capability_fingerprint != definition.capability_fingerprint
            or _provider_descriptor_digest(snapshot.provider_descriptor) != definition.provider_descriptor_digest
        ):
            raise ValueError("recovered PendingAct tool composition cannot be reconstructed exactly")
        live_definitions = {item.name: item for item in snapshot.catalog.definitions}
        actions = []
        for pending in frontier.actions:
            live_definition = live_definitions.get(pending.tool_name)
            if (
                live_definition is None
                or live_definition.semantic_identity != pending.definition_identity
                or ToolEffect(live_definition.effect) is not pending.effect
            ):
                raise ValueError("recovered PendingAct tool definition does not match its durable identity")
            revisions = state.pending_action_arguments_by_invocation.get(pending.invocation_id, ())
            if not revisions or revisions[-1].revision != pending.current_arguments_revision:
                raise ValueError("recovered PendingAct argument revision is unavailable")
            actions.append(
                ToolCallAction(
                    action_id=pending.action_id,
                    name=pending.tool_name,
                    arguments=revisions[-1].arguments,
                )
            )
        completed = frozenset(
            invocation.value
            for invocation in state.pending_action_result_by_invocation
            if invocation in {action.invocation_id for action in frontier.actions}
        )
        skipped = frozenset(
            invocation.value
            for invocation in state.skipped_pending_actions
            if invocation in {action.invocation_id for action in frontier.actions}
        )
        committed_message_ids = {
            event.message_id
            for invocation, event in state.pending_action_result_by_invocation.items()
            if invocation.value in completed
        }
        return PendingActResume(
            frontier,
            tuple(actions),
            completed,
            skipped,
            tuple(message for message in state.transcript_messages if message.id in committed_message_ids),
        )

    async def begin_external_effect(
        self,
        acceptance: PendingActAcceptance,
        ordinal: int,
        identity,
    ) -> ExternalEffectPermit:
        frontier = acceptance.frontier
        action = frontier.actions[ordinal]
        if action.invocation_id != identity.invocation_id or action.effect is not ToolEffect.EXTERNAL:
            raise ValueError("external effect permit does not match the PendingAct action")
        await self._ensure_claim(frontier)
        expected_version = self._projection.snapshot().through_sequence
        assert self._claim is not None
        self._claim_service.begin_invoke(
            self._claim,
            identity.invocation_id,
            frontier_revision=frontier.revision,
            expected_stream_version=expected_version,
            at=datetime.now(timezone.utc),
        )
        writer = self._writer_guard.writer_for(self._run_id, self._fencing_token)
        await self._service.start_external_effect(
            frontier.frontier_id,
            identity,
            _approval_request_for(self._projection.snapshot(), action.invocation_id),
            frontier_revision=frontier.revision,
            claim_fencing_token=self._claim.fencing_token,
            expected_stream_version=expected_version,
            writer=writer,
        )
        return ExternalEffectPermit(frontier, identity)

    async def begin_invoke(
        self,
        acceptance: PendingActAcceptance,
        ordinal: int,
        identity,
    ) -> PendingActInvokePermit:
        frontier = acceptance.frontier
        action = frontier.actions[ordinal]
        if action.invocation_id != identity.invocation_id:
            raise ValueError("invoke permit does not match the PendingAct action")
        await self._ensure_claim(frontier)
        expected_version = self._projection.snapshot().through_sequence
        assert self._claim is not None
        permit = self._claim_service.begin_invoke(
            self._claim,
            identity.invocation_id,
            frontier_revision=frontier.revision,
            expected_stream_version=expected_version,
            at=datetime.now(timezone.utc),
        )
        return PendingActInvokePermit(
            permit.claim_id,
            permit.frontier_id,
            permit.owner_id,
            permit.incarnation_id,
            permit.claim_revision,
            permit.fencing_token,
            permit.frontier_revision,
            permit.invocation_id,
            action.fileops_transaction_id,
        )

    async def mark_external_effect_in_doubt(
        self,
        permit: ExternalEffectPermit,
        *,
        evidence: JsonValue,
    ) -> None:
        expected_version = self._projection.snapshot().through_sequence
        writer = self._writer_guard.writer_for(self._run_id, self._fencing_token)
        await self._service.mark_external_effect_in_doubt(
            permit.frontier.frontier_id,
            permit.identity.invocation_id,
            evidence,
            expected_stream_version=expected_version,
            writer=writer,
        )

    async def resolve_approval(
        self, acceptance: PendingActAcceptance, intent: ToolApprovalIntent
    ) -> ToolApprovalResolution:
        frontier = acceptance.frontier
        action = next(
            (item for item in frontier.actions if item.invocation_id == intent.identity.invocation_id),
            None,
        )
        if action is None:
            raise ValueError("approval invocation is absent from PendingAct")
        state = self._projection.snapshot()
        arguments = state.pending_action_arguments_by_invocation[action.invocation_id][-1]
        writer = self._writer_guard.writer_for(self._run_id, self._fencing_token)
        request = await self._approval.request(
            frontier,
            action.ordinal,
            arguments,
            ApprovalRequest(
                tool_name=intent.tool_name,
                target="\n  ".join(intent.permission_targets),
                paths=list(intent.permission_targets),
                reason_detail=intent.reason,
                mutates_fs=intent.mutates_fs,
            ),
            permission_targets_digest=_permission_targets_digest(intent.permission_targets),
            writer=writer,
        )
        approval_state = self._projection.snapshot().approval_by_request_id.get(request.request_id)
        if approval_state is not None and approval_state.state is not ApprovalState.WAITING:
            decision = await self._approval.decide(request, ApprovalChoice.reject(), writer=writer)
        else:
            choice = await self._request_approval(request)
            decision = await self._approval.decide(request, choice, writer=writer)
        if decision.arguments is not None and dict(decision.arguments) != dict(arguments.arguments):
            revised_arguments = dict(decision.arguments)
            revised_digest = tool_arguments_digest(revised_arguments)
            await self._service.revise_arguments(
                frontier.frontier_id,
                PendingActionArgumentsRevision(
                    action.invocation_id,
                    arguments.revision + 1,
                    revised_arguments,
                    revised_digest,
                ),
                previous_arguments_digest=arguments.arguments_digest,
                expected_stream_version=self._projection.snapshot().through_sequence,
                writer=writer,
                cancelled_approval_request_id=decision.request_id,
            )
            return ToolApprovalResolution(False, False, decision.request_id, revised_arguments)
        return ToolApprovalResolution(
            decision.disposition in {ApprovalDisposition.ALLOW_ONCE, ApprovalDisposition.ALLOW_SESSION},
            decision.disposition is ApprovalDisposition.ALLOW_SESSION,
            decision.request_id,
        )

    async def _acquire_claim(self, frontier: PendingActFrontier) -> None:
        if self._claim is not None:
            return
        now = datetime.now(timezone.utc)
        expected_version = self._projection.snapshot().through_sequence
        self._claim = await self._claim_service.acquire(
            frontier.frontier_id,
            self._writer_guard.owner_id,
            self._writer_guard.incarnation_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
            expected_stream_version=expected_version,
            writer=self._writer_guard.writer_for(self._run_id, self._fencing_token),
        )

    async def _ensure_claim(self, frontier: PendingActFrontier) -> None:
        if self._claim is None:
            await self._acquire_claim(frontier)

    async def _release_claim(self) -> None:
        claim, self._claim = self._claim, None
        if claim is None:
            return
        expected_version = self._projection.snapshot().through_sequence
        await self._claim_service.release(
            claim,
            expected_stream_version=expected_version,
            writer=self._writer_guard.writer_for(self._run_id, self._fencing_token),
        )


def _definition_ref(snapshot: ToolBindingSnapshot) -> ToolCompositionDefinitionRef:
    return ToolCompositionDefinitionRef(
        blueprint_identity=snapshot.catalog.identity.catalog_id,
        blueprint_version=snapshot.catalog.identity.version,
        executable_digest=snapshot.catalog.fingerprint,
        composition_generation_id=snapshot.composition_generation_id,
        catalog_fingerprint=snapshot.catalog.fingerprint,
        provider_descriptor_digest=_provider_descriptor_digest(snapshot.provider_descriptor),
        policy_generation=snapshot.composition_generation_id,
        capability_fingerprint=snapshot.capability_fingerprint,
    )


def _provider_descriptor_digest(descriptor: str) -> str:
    return f"sha256-{hashlib.sha256(descriptor.encode('utf-8')).hexdigest()}"


def _permission_targets_digest(targets: tuple[str, ...]) -> str:
    encoded = chr(0).join(targets).encode("utf-8")
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


def _fileops_transaction_id(frontier_id: PendingActFrontierId, invocation_id: ToolInvocationId) -> str:
    encoded = f"{frontier_id.value}|{invocation_id.value}|fileops".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approval_request_for(snapshot: SessionProjectionState, invocation_id: ToolInvocationId):
    matches = tuple(
        request_id
        for request_id, approval in snapshot.approval_by_request_id.items()
        if approval.request.invocation_id == invocation_id
    )
    if len(matches) > 1:
        raise ValueError("PendingAct invocation has multiple approval requests")
    return matches[0] if matches else None


__all__ = ["RuntimePendingActAcceptance"]
