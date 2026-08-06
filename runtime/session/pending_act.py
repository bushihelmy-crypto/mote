"""Guarded Session commands for the canonical PendingAct fact chain."""

from __future__ import annotations

from mote.contracts.events.envelope import JsonValue
from mote.contracts.events.pending_act import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
    ExternalEffectFinishedEvent,
    ExternalEffectInDoubtEvent,
    ExternalEffectStartedEvent,
    PendingActCreatedEvent,
    PendingActionArgumentsRevisedEvent,
    PendingActionResultCommittedEvent,
    PendingActionsSkippedEvent,
    PendingActSchemaActivatedEvent,
    PendingActSettledEvent,
    RunRecoveryCursorAdvancedEvent,
    SessionPermissionRuleGrantedEvent,
)
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingActionArgumentsRevision,
)
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.interaction.approval import ApprovalDisposition, ApprovalRequest, ApprovalState
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.events.journal import AppendResult, StreamWriterFence
from mote.contracts.ports.session.facts import GuardedSessionFactBatch, GuardedSessionFactSink, RolloutSourceEvent
from mote.contracts.tool.external_effect import ExternalEffectState, ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationId, ToolInvocationIdentity
from mote.runtime.session.projection import SessionLiveProjection, SessionProjectionState


class RuntimePendingActService:
    """Apply exact-version PendingAct commands without hidden retries."""

    def __init__(self, projection: SessionLiveProjection, sink: GuardedSessionFactSink) -> None:
        self._projection = projection
        self._sink = sink

    async def create(
        self,
        frontier: PendingActFrontier,
        arguments: tuple[PendingActionArgumentsRevision, ...],
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
        leading_events: tuple[RolloutSourceEvent, ...] = (),
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        if frontier.run_id in snapshot.active_pending_act_by_run:
            raise ValueError("run already has an active PendingAct")
        if len(arguments) != len(frontier.actions):
            raise ValueError("A0 requires one initial argument revision per action")
        by_invocation = {revision.invocation_id: revision for revision in arguments}
        if len(by_invocation) != len(arguments):
            raise ValueError("A0 argument revision identities must be unique")
        if set(by_invocation) != {action.invocation_id for action in frontier.actions}:
            raise ValueError("A0 argument revisions do not match frontier actions")
        if any(revision.revision != 0 for revision in arguments):
            raise ValueError("A0 argument revisions must start at zero")

        events: list[RolloutSourceEvent] = list(leading_events)
        if not snapshot.pending_act_schema_activated:
            events.append(PendingActSchemaActivatedEvent(frontier.run_id))
        events.append(PendingActCreatedEvent(frontier))
        events.extend(
            PendingActionArgumentsRevisedEvent(
                frontier.frontier_id,
                by_invocation[action.invocation_id],
                None,
            )
            for action in frontier.actions
        )
        events.append(
            RunRecoveryCursorAdvancedEvent(
                RunRecoveryCursor(
                    frontier.run_id,
                    0,
                    RecoveryTarget.ACT,
                    frontier.frontier_id,
                    False,
                )
            )
        )
        return await self._sink.commit_guarded(GuardedSessionFactBatch(tuple(events), expected_stream_version, writer))

    async def request_approval(
        self,
        request: ApprovalRequest,
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        if request.request_id is None or request.frontier_id is None or request.invocation_id is None:
            raise ValueError("approval request identity is incomplete")
        if request.request_id in snapshot.approval_by_request_id:
            raise ValueError("approval request identity already exists")
        return await self._sink.commit_guarded(
            GuardedSessionFactBatch((ApprovalRequestedEvent(request),), expected_stream_version, writer)
        )

    async def revise_arguments(
        self,
        frontier_id: PendingActFrontierId,
        revision: PendingActionArgumentsRevision,
        *,
        previous_arguments_digest: str,
        expected_stream_version: int,
        writer: StreamWriterFence,
        cancelled_approval_request_id: ApprovalRequestId | None = None,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        frontier = snapshot.pending_act_by_id.get(frontier_id)
        if frontier is None or revision.invocation_id not in {action.invocation_id for action in frontier.actions}:
            raise ValueError("argument revision references an unknown PendingAct action")
        prior = snapshot.pending_action_arguments_by_invocation.get(revision.invocation_id, ())
        action = next(item for item in frontier.actions if item.invocation_id == revision.invocation_id)
        if (
            not prior
            or revision.revision != prior[-1].revision + 1
            or prior[-1].arguments_digest != previous_arguments_digest
        ):
            raise ValueError("argument revision is not the next exact revision")
        if action.effect.value == "local":
            raise ValueError("LOCAL action arguments cannot change after its FileOps transaction identity is fixed")
        events = (
            (
                ApprovalDecisionCommittedEvent(
                    cancelled_approval_request_id,
                    ApprovalDisposition.CANCEL,
                    prior[-1].revision,
                    prior[-1].arguments_digest,
                ),
            )
            if cancelled_approval_request_id is not None
            else ()
        ) + (PendingActionArgumentsRevisedEvent(frontier_id, revision, previous_arguments_digest),)
        return await self._sink.commit_guarded(
            GuardedSessionFactBatch(
                events,
                expected_stream_version,
                writer,
            )
        )

    async def decide_approval(
        self,
        request_id: ApprovalRequestId,
        disposition: ApprovalDisposition,
        *,
        arguments_revision: int,
        arguments_digest: str,
        expected_stream_version: int,
        writer: StreamWriterFence,
        session_rule: SessionPermissionRuleGrantedEvent | None = None,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        approval = snapshot.approval_by_request_id.get(request_id)
        if approval is None or approval.state is not ApprovalState.WAITING:
            raise ValueError("approval request is not waiting")
        if session_rule is not None:
            if disposition is not ApprovalDisposition.ALLOW_SESSION:
                raise ValueError("session rule requires allow-session disposition")
            if session_rule.request_id != request_id:
                raise ValueError("session rule request identity mismatch")
        event = ApprovalDecisionCommittedEvent(request_id, disposition, arguments_revision, arguments_digest)
        events = (event,) + ((session_rule,) if session_rule is not None else ())
        return await self._sink.commit_guarded(GuardedSessionFactBatch(events, expected_stream_version, writer))

    async def start_external_effect(
        self,
        frontier_id: PendingActFrontierId,
        identity: ToolInvocationIdentity,
        approval_request_id: ApprovalRequestId | None,
        *,
        frontier_revision: int,
        claim_fencing_token: int,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        self._snapshot(expected_stream_version)
        event = ExternalEffectStartedEvent(
            frontier_id,
            identity,
            approval_request_id,
            frontier_revision,
            claim_fencing_token,
        )
        return await self._sink.commit_guarded(GuardedSessionFactBatch((event,), expected_stream_version, writer))

    async def settle(
        self,
        frontier_id: PendingActFrontierId,
        result_events: tuple[RolloutSourceEvent, ...],
        *,
        expected_frontier_revision: int,
        continue_inference: bool,
        expected_stream_version: int,
        writer: StreamWriterFence,
        effect_receipts: tuple[ToolEffectReceipt, ...] = (),
        action_results: tuple[PendingActionResultCommittedEvent, ...] = (),
        skipped: PendingActionsSkippedEvent | None = None,
        rejected_approval_request_id: ApprovalRequestId | None = None,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        frontier = snapshot.pending_act_by_id.get(frontier_id)
        if frontier is None or frontier.revision != expected_frontier_revision:
            raise ValueError("PendingAct settlement frontier revision mismatch")
        if snapshot.active_pending_act_by_run.get(frontier.run_id) != frontier_id:
            raise ValueError("PendingAct settlement does not own the active run frontier")
        prior_cursor = snapshot.run_cursor_by_run_id.get(frontier.run_id)
        if prior_cursor is None or prior_cursor.pending_act_id != frontier_id:
            raise ValueError("PendingAct settlement requires its active ACT cursor")
        for receipt in effect_receipts:
            effect = snapshot.external_effect_by_invocation.get(receipt.identity.invocation_id)
            if effect is None or effect.state is not ExternalEffectState.STARTED:
                raise ValueError("external effect receipt requires STARTED")
            if receipt.identity.invocation_id not in {action.invocation_id for action in frontier.actions}:
                raise ValueError("external effect receipt does not belong to this PendingAct")
        result_invocations = {result.invocation_id for result in action_results}
        if len(result_invocations) != len(action_results):
            raise ValueError("PendingAct settlement contains duplicate action results")
        frontier_invocations = {action.invocation_id for action in frontier.actions}
        if not result_invocations.issubset(frontier_invocations):
            raise ValueError("PendingAct settlement contains an unknown action result")
        skipped_invocations = set(skipped.invocation_ids) if skipped is not None else set()
        if skipped is not None and skipped.frontier_id != frontier_id:
            raise ValueError("skipped actions belong to another PendingAct")
        if result_invocations & skipped_invocations:
            raise ValueError("PendingAct action cannot be both completed and skipped")
        if result_invocations | skipped_invocations != frontier_invocations:
            raise ValueError("PendingAct settlement must account for every action")
        if rejected_approval_request_id is not None:
            rejected = snapshot.approval_by_request_id.get(rejected_approval_request_id)
            if rejected is None or rejected.state is not ApprovalState.WAITING:
                raise ValueError("rejected approval must reference a waiting request")
            if rejected.request.frontier_id != frontier_id:
                raise ValueError("rejected approval belongs to another PendingAct")
        events = (
            (
                (
                    ApprovalDecisionCommittedEvent(
                        rejected_approval_request_id,
                        ApprovalDisposition.REJECT,
                        snapshot.approval_by_request_id[rejected_approval_request_id].request.arguments_revision,
                        snapshot.approval_by_request_id[rejected_approval_request_id].request.arguments_digest,
                    ),
                )
                if rejected_approval_request_id is not None
                else ()
            )
            + tuple(ExternalEffectFinishedEvent(frontier_id, receipt) for receipt in effect_receipts)
            + result_events
            + action_results
            + ((skipped,) if skipped is not None else ())
            + (
                PendingActSettledEvent(frontier_id, expected_frontier_revision),
                RunRecoveryCursorAdvancedEvent(
                    RunRecoveryCursor(
                        frontier.run_id,
                        prior_cursor.revision + 1,
                        RecoveryTarget.OBSERVE,
                        None,
                        continue_inference,
                    )
                ),
            )
        )
        return await self._sink.commit_guarded(GuardedSessionFactBatch(events, expected_stream_version, writer))

    async def mark_external_effect_in_doubt(
        self,
        frontier_id: PendingActFrontierId,
        invocation_id: ToolInvocationId,
        evidence: JsonValue,
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        self._snapshot(expected_stream_version)
        return await self._sink.commit_guarded(
            GuardedSessionFactBatch(
                (ExternalEffectInDoubtEvent(frontier_id, invocation_id, evidence),),
                expected_stream_version,
                writer,
            )
        )

    async def mark_external_effects_in_doubt(
        self,
        frontier_id: PendingActFrontierId,
        evidence_by_invocation: tuple[tuple[ToolInvocationId, JsonValue], ...],
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        frontier = snapshot.pending_act_by_id.get(frontier_id)
        if frontier is None:
            raise ValueError("in-doubt settlement references an unknown PendingAct")
        action_ids = {action.invocation_id for action in frontier.actions}
        invocation_ids = tuple(item[0] for item in evidence_by_invocation)
        if len(set(invocation_ids)) != len(invocation_ids):
            raise ValueError("in-doubt settlement contains duplicate invocations")
        if not set(invocation_ids).issubset(action_ids):
            raise ValueError("in-doubt settlement references an unknown action")
        events = tuple(
            ExternalEffectInDoubtEvent(frontier_id, invocation_id, evidence)
            for invocation_id, evidence in evidence_by_invocation
        )
        return await self._sink.commit_guarded(GuardedSessionFactBatch(events, expected_stream_version, writer))

    async def commit_reconciled_external_result(
        self,
        frontier_id: PendingActFrontierId,
        receipt: ToolEffectReceipt,
        result_event: RolloutSourceEvent,
        action_result: PendingActionResultCommittedEvent,
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        snapshot = self._snapshot(expected_stream_version)
        frontier = snapshot.pending_act_by_id.get(frontier_id)
        invocation_id = receipt.identity.invocation_id
        if frontier is None or invocation_id not in {action.invocation_id for action in frontier.actions}:
            raise ValueError("reconciled result references an unknown action")
        if action_result.invocation_id != invocation_id:
            raise ValueError("reconciled action result identity mismatch")
        if (
            action_result.receipt_id != receipt.receipt_id
            or action_result.presentation_digest != receipt.presentation_digest
        ):
            raise ValueError("reconciled action result does not bind its receipt")
        effect = snapshot.external_effect_by_invocation.get(invocation_id)
        if effect is None or effect.state is not ExternalEffectState.STARTED:
            raise ValueError("reconciled receipt requires a STARTED effect")
        return await self._sink.commit_guarded(
            GuardedSessionFactBatch(
                (
                    ExternalEffectFinishedEvent(frontier_id, receipt),
                    result_event,
                    action_result,
                ),
                expected_stream_version,
                writer,
            )
        )

    def _snapshot(self, expected_stream_version: int) -> SessionProjectionState:
        snapshot = self._projection.snapshot()
        if snapshot.through_sequence != expected_stream_version:
            raise ValueError("PendingAct command snapshot is not at the expected stream version")
        return snapshot


__all__ = ["RuntimePendingActService"]
