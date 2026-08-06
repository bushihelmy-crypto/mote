from __future__ import annotations

import pytest

from mote.contracts.events.pending_act import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
    SessionPermissionRuleGrantedEvent,
)
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    ToolCompositionDefinitionRef,
)
from mote.contracts.interaction.approval import ApprovalChoice, ApprovalDisposition, ApprovalRequest, ApprovalState
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.tool import ToolEffect, ToolInvocationId
from mote.runtime.session.durable_approval import DurableApprovalCoordinator, deterministic_approval_request_id
from mote.runtime.session.projection import ApprovalProjection, SessionLiveProjection, SessionProjectionState


def _frontier() -> PendingActFrontier:
    return PendingActFrontier(
        1,
        PendingActFrontierId("frontier-1"),
        "session-1",
        "run-1",
        "call-1",
        0,
        ToolCompositionDefinitionRef(
            "agent",
            "1",
            "sha",
            "generation",
            "catalog",
            "provider",
            "policy",
            "capability",
        ),
        (
            PendingAction(
                0,
                ToolInvocationId("invocation-1"),
                "action-1",
                "Read",
                "read/v1",
                1,
                ToolEffect.PURE,
                0,
            ),
        ),
    )


def test_approval_request_identity_is_deterministic_and_revision_bound() -> None:
    frontier = _frontier()
    first = deterministic_approval_request_id(frontier, "invocation-1", 0, "sha256-args", "sha256-targets")
    repeated = deterministic_approval_request_id(frontier, "invocation-1", 0, "sha256-args", "sha256-targets")
    revised = deterministic_approval_request_id(frontier, "invocation-1", 1, "sha256-new-args", "sha256-targets")

    assert repeated == first
    assert revised != first


class _Sink:
    def __init__(self) -> None:
        self.batches = []

    async def commit_guarded(self, batch):
        self.batches.append(batch)
        return object()


@pytest.mark.asyncio
async def test_response_loss_returns_the_canonical_terminal_decision() -> None:
    request_id = ApprovalRequestId("approval-1")
    request = ApprovalRequest(
        tool_name="Read",
        request_id=request_id,
        frontier_id=PendingActFrontierId("frontier-1"),
        invocation_id=ToolInvocationId("invocation-1"),
        arguments_digest="args",
        permission_targets_digest="targets",
    )
    projection = SessionLiveProjection("session:test")
    projection._state = SessionProjectionState(
        approval_by_request_id={
            request_id: ApprovalProjection(request, ApprovalState.APPROVED, ApprovalDisposition.ALLOW_ONCE)
        }
    )
    sink = _Sink()

    decision = await DurableApprovalCoordinator(projection, sink).decide(
        request,
        ApprovalChoice.reject(),
        writer=StreamWriterFence("run-1", "owner", "incarnation", 1),
    )

    assert decision.disposition is ApprovalDisposition.ALLOW_ONCE
    assert sink.batches == []


@pytest.mark.asyncio
async def test_allow_session_commits_decision_and_rule_in_one_batch() -> None:
    request_id = ApprovalRequestId("approval-session")
    request = ApprovalRequest(
        tool_name="Write",
        paths=["/workspace/file.txt"],
        mutates_fs=True,
        request_id=request_id,
        frontier_id=PendingActFrontierId("frontier-1"),
        invocation_id=ToolInvocationId("invocation-1"),
        arguments_digest="args",
        permission_targets_digest="targets",
    )
    projection = SessionLiveProjection("session:test")
    projection._state = SessionProjectionState(
        approval_by_request_id={request_id: ApprovalProjection(request, ApprovalState.WAITING)}
    )
    sink = _Sink()

    await DurableApprovalCoordinator(projection, sink).decide(
        request,
        ApprovalChoice.allow_session(),
        writer=StreamWriterFence("run-1", "owner", "incarnation", 1),
    )

    assert isinstance(sink.batches[0].events[0], ApprovalDecisionCommittedEvent)
    assert isinstance(sink.batches[0].events[1], SessionPermissionRuleGrantedEvent)
    assert sink.batches[0].events[1].mutates_fs is True
