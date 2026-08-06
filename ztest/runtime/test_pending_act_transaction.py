from __future__ import annotations

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.contracts.events.pending_act import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
    PendingActCreatedEvent,
    PendingActionArgumentsRevisedEvent,
    PendingActionResultCommittedEvent,
    RunRecoveryCursorAdvancedEvent,
)
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.interaction.approval import ApprovalRequest
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.tool import ToolEffect, ToolInvocationId, tool_arguments_digest
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.pending_act import RuntimePendingActService
from mote.runtime.session.projection import SessionLiveProjection


class _Sink:
    def __init__(self) -> None:
        self.batch = None

    async def commit_guarded(self, batch):
        self.batch = batch
        return object()


def _frontier() -> PendingActFrontier:
    return PendingActFrontier(
        1,
        PendingActFrontierId("frontier-1"),
        "session-1",
        "run-1",
        "model-call-1",
        0,
        ToolCompositionDefinitionRef(
            "agent",
            "1",
            "sha256-executable",
            "generation-1",
            "sha256-catalog",
            "sha256-provider",
            "policy-1",
            "sha256-capability",
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


@pytest.mark.asyncio
async def test_a0_builds_one_guarded_atomic_fact_batch(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    service = RuntimePendingActService(projection, sink)
    arguments = {"path": "README.md"}
    revision = PendingActionArgumentsRevision(
        ToolInvocationId("invocation-1"), 0, arguments, tool_arguments_digest(arguments)
    )
    writer = StreamWriterFence("run-1", "worker-1", "incarnation-1", 1)

    leading = (MessageAppendedEvent(AIMessage(content="tool call")),)
    await service.create(
        _frontier(),
        (revision,),
        expected_stream_version=1,
        writer=writer,
        leading_events=leading,
    )

    assert sink.batch is not None
    assert sink.batch.expected_stream_version == 1
    assert sink.batch.writer == writer
    assert [type(event).__name__ for event in sink.batch.events] == [
        "MessageAppendedEvent",
        "PendingActSchemaActivatedEvent",
        "PendingActCreatedEvent",
        "PendingActionArgumentsRevisedEvent",
        "RunRecoveryCursorAdvancedEvent",
    ]


@pytest.mark.asyncio
async def test_a0_rejects_a_stale_projection_without_writing(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    service = RuntimePendingActService(projection, sink)
    arguments = {}
    revision = PendingActionArgumentsRevision(
        ToolInvocationId("invocation-1"), 0, arguments, tool_arguments_digest(arguments)
    )

    with pytest.raises(ValueError, match="expected stream version"):
        await service.create(
            _frontier(),
            (revision,),
            expected_stream_version=2,
            writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        )
    assert sink.batch is None


@pytest.mark.asyncio
async def test_argument_revision_cancels_old_approval_in_the_same_batch(
    tmp_path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    frontier = _frontier()
    old_arguments = {"path": "old"}
    old_digest = tool_arguments_digest(old_arguments)
    old_revision = PendingActionArgumentsRevision(frontier.actions[0].invocation_id, 0, old_arguments, old_digest)
    request_id = ApprovalRequestId("approval-old")
    log.commit_offline(PendingActCreatedEvent(frontier))
    log.commit_offline(PendingActionArgumentsRevisedEvent(frontier.frontier_id, old_revision, None))
    log.commit_offline(
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False))
    )
    log.commit_offline(
        ApprovalRequestedEvent(
            ApprovalRequest(
                tool_name="Read",
                request_id=request_id,
                frontier_id=frontier.frontier_id,
                invocation_id=frontier.actions[0].invocation_id,
                arguments_digest=old_digest,
                permission_targets_digest="targets",
            )
        )
    )
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    new_arguments = {"path": "new"}

    await RuntimePendingActService(projection, sink).revise_arguments(
        frontier.frontier_id,
        PendingActionArgumentsRevision(
            frontier.actions[0].invocation_id,
            1,
            new_arguments,
            tool_arguments_digest(new_arguments),
        ),
        previous_arguments_digest=old_digest,
        expected_stream_version=5,
        writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        cancelled_approval_request_id=request_id,
    )

    assert [type(event).__name__ for event in sink.batch.events] == [
        "ApprovalDecisionCommittedEvent",
        "PendingActionArgumentsRevisedEvent",
    ]
    assert sink.batch.events[0].state.value == "cancelled"


@pytest.mark.asyncio
async def test_settlement_commits_results_frontier_and_observe_cursor_together(
    tmp_path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    frontier = _frontier()
    arguments = {}
    revision = PendingActionArgumentsRevision(
        ToolInvocationId("invocation-1"), 0, arguments, tool_arguments_digest(arguments)
    )
    log.commit_offline(PendingActCreatedEvent(frontier))
    log.commit_offline(PendingActionArgumentsRevisedEvent(frontier.frontier_id, revision, None))
    log.commit_offline(
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False))
    )
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    service = RuntimePendingActService(projection, sink)
    result = MessageAppendedEvent(AIMessage(content="tool result"))

    await service.settle(
        frontier.frontier_id,
        (result,),
        expected_frontier_revision=0,
        continue_inference=True,
        expected_stream_version=4,
        writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        action_results=(
            PendingActionResultCommittedEvent(
                frontier.frontier_id,
                frontier.actions[0].invocation_id,
                result.message.id,
            ),
        ),
    )

    assert sink.batch is not None
    assert [type(event).__name__ for event in sink.batch.events] == [
        "MessageAppendedEvent",
        "PendingActionResultCommittedEvent",
        "PendingActSettledEvent",
        "RunRecoveryCursorAdvancedEvent",
    ]
    cursor = sink.batch.events[-1].cursor
    assert cursor.next_node is RecoveryTarget.OBSERVE
    assert cursor.continue_inference is True


@pytest.mark.asyncio
async def test_rejected_approval_is_committed_in_the_terminal_b_batch(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    frontier = _frontier()
    arguments = {}
    digest = tool_arguments_digest(arguments)
    revision = PendingActionArgumentsRevision(ToolInvocationId("invocation-1"), 0, arguments, digest)
    request_id = ApprovalRequestId("approval-1")
    log.commit_offline(PendingActCreatedEvent(frontier))
    log.commit_offline(PendingActionArgumentsRevisedEvent(frontier.frontier_id, revision, None))
    log.commit_offline(
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False))
    )
    log.commit_offline(
        ApprovalRequestedEvent(
            ApprovalRequest(
                tool_name="Read",
                request_id=request_id,
                frontier_id=frontier.frontier_id,
                invocation_id=frontier.actions[0].invocation_id,
                arguments_revision=0,
                arguments_digest=digest,
                permission_targets_digest="targets",
            )
        )
    )
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    result = MessageAppendedEvent(AIMessage(content="rejected"))

    await RuntimePendingActService(projection, sink).settle(
        frontier.frontier_id,
        (result,),
        expected_frontier_revision=0,
        continue_inference=True,
        expected_stream_version=5,
        writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        action_results=(
            PendingActionResultCommittedEvent(
                frontier.frontier_id,
                frontier.actions[0].invocation_id,
                result.message.id,
            ),
        ),
        rejected_approval_request_id=request_id,
    )

    assert isinstance(sink.batch.events[0], ApprovalDecisionCommittedEvent)
    assert [type(event).__name__ for event in sink.batch.events[-2:]] == [
        "PendingActSettledEvent",
        "RunRecoveryCursorAdvancedEvent",
    ]
