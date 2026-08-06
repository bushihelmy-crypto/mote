"""Durable approval request/decision coordinator for PendingAct actions."""

from __future__ import annotations

import hashlib

from mote.contracts.events.pending_act import SessionPermissionRuleGrantedEvent
from mote.contracts.execution.pending_act import PendingActFrontier, PendingActionArgumentsRevision
from mote.contracts.interaction.approval import (
    ApprovalChoice,
    ApprovalDisposition,
    ApprovalRequest,
    DurableApprovalDecision,
)
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.ports.session.facts import GuardedSessionFactSink
from mote.runtime.session.pending_act import RuntimePendingActService
from mote.runtime.session.projection import SessionLiveProjection


class DurableApprovalCoordinator:
    def __init__(self, projection: SessionLiveProjection, sink: GuardedSessionFactSink) -> None:
        self._projection = projection
        self._service = RuntimePendingActService(projection, sink)

    async def request(
        self,
        frontier: PendingActFrontier,
        ordinal: int,
        arguments: PendingActionArgumentsRevision,
        template: ApprovalRequest,
        *,
        permission_targets_digest: str,
        writer: StreamWriterFence,
    ) -> ApprovalRequest:
        action = frontier.actions[ordinal]
        request_id = deterministic_approval_request_id(
            frontier,
            action.invocation_id.value,
            arguments.revision,
            arguments.arguments_digest,
            permission_targets_digest,
        )
        state = self._projection.snapshot()
        prior = state.approval_by_request_id.get(request_id)
        if prior is not None:
            return prior.request
        request = ApprovalRequest(
            tool_name=template.tool_name,
            kind=template.kind,
            target=template.target,
            paths=list(template.paths),
            risk=template.risk,
            reason_code=template.reason_code,
            reason_detail=template.reason_detail,
            suggestion=template.suggestion,
            mutates_fs=template.mutates_fs,
            request_id=request_id,
            frontier_id=frontier.frontier_id,
            invocation_id=action.invocation_id,
            arguments_revision=arguments.revision,
            arguments_digest=arguments.arguments_digest,
            permission_targets_digest=permission_targets_digest,
            expected_frontier_revision=frontier.revision,
        )
        await self._service.request_approval(request, expected_stream_version=state.through_sequence, writer=writer)
        return request

    async def decide(
        self,
        request: ApprovalRequest,
        choice: ApprovalChoice,
        *,
        writer: StreamWriterFence,
    ) -> DurableApprovalDecision:
        if request.request_id is None:
            raise ValueError("durable approval decision requires a request identity")
        disposition = choice.disposition
        state = self._projection.snapshot()
        approval = state.approval_by_request_id.get(request.request_id)
        if approval is not None and approval.state.value != "waiting":
            canonical = {
                "approved": (
                    ApprovalDisposition.ALLOW_SESSION
                    if approval.disposition is ApprovalDisposition.ALLOW_SESSION
                    else ApprovalDisposition.ALLOW_ONCE
                ),
                "rejected": ApprovalDisposition.REJECT,
                "cancelled": ApprovalDisposition.CANCEL,
            }[approval.state.value]
            return DurableApprovalDecision(request.request_id, canonical)
        if disposition in {ApprovalDisposition.REJECT, ApprovalDisposition.CANCEL}:
            return DurableApprovalDecision(request.request_id, disposition)
        if choice.arguments is not None:
            return DurableApprovalDecision(request.request_id, disposition, arguments=choice.arguments)
        await self._service.decide_approval(
            request.request_id,
            disposition,
            arguments_revision=request.arguments_revision,
            arguments_digest=request.arguments_digest,
            expected_stream_version=state.through_sequence,
            writer=writer,
            session_rule=(
                SessionPermissionRuleGrantedEvent(
                    request.request_id,
                    request.tool_name,
                    tuple(request.paths),
                    request.mutates_fs,
                )
                if disposition is ApprovalDisposition.ALLOW_SESSION
                else None
            ),
        )
        return DurableApprovalDecision(request.request_id, disposition)


def deterministic_approval_request_id(
    frontier: PendingActFrontier,
    invocation_id: str,
    arguments_revision: int,
    arguments_digest: str,
    permission_targets_digest: str,
) -> ApprovalRequestId:
    value = "|".join(
        (
            frontier.frontier_id.value,
            invocation_id,
            str(arguments_revision),
            arguments_digest,
            permission_targets_digest,
        )
    )
    return ApprovalRequestId(hashlib.sha256(value.encode("utf-8")).hexdigest())


__all__ = ["DurableApprovalCoordinator", "deterministic_approval_request_id"]
