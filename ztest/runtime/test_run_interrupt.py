from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.conversation import ToolMessage, UserMessage
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.contracts.events.pending_act import (
    PendingActCreatedEvent,
    PendingActionArgumentsRevisedEvent,
    PendingActionResultCommittedEvent,
    RunRecoveryCursorAdvancedEvent,
    TurnInterruptedContextAttachedEvent,
    TurnInterruptedEvent,
)
from mote.contracts.execution.interrupt_context import TURN_ABORTED_FRAGMENT
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.tool import ToolEffect, ToolInvocationId, tool_arguments_digest
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.projection import SessionLiveProjection, SessionProjectionState, reduce_session_event
from mote.runtime.session.run_interrupt import RunInterruptService


class _Sink:
    def __init__(self) -> None:
        self.batches = []

    async def commit_guarded(self, batch):
        self.batches.append(batch)
        return object()


@pytest.mark.asyncio
async def test_interrupt_is_durable_before_process_cancel_permit(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    writer = StreamWriterFence("run-1", "worker-1", "incarnation-1", 7)
    now = datetime.now(timezone.utc)

    permit = await RunInterruptService(projection, sink).interrupt_run(
        "run-1",
        model_call_id="model-call-1",
        interrupted_at=now,
        expected_stream_version=1,
        writer=writer,
    )

    assert isinstance(sink.batches[0].events[0], TurnInterruptedEvent)
    assert permit.run_id == "run-1"
    assert permit.fencing_token == 7


@pytest.mark.asyncio
async def test_interrupt_settlement_requires_durable_interrupt(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    message = UserMessage(content="request")
    log.commit_offline(MessageEvent(message))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    service = RunInterruptService(projection, _Sink())
    writer = StreamWriterFence("run-1", "worker-1", "incarnation-1", 1)

    from mote.contracts.execution.interrupt import RunInterruptPermit

    with pytest.raises(ValueError, match="no durable interrupt"):
        await service.settle(
            RunInterruptPermit("run-1", "worker-1", "incarnation-1", 1, datetime.now(timezone.utc)),
            anchor_message_id=message.id,
            expected_stream_version=2,
            writer=writer,
        )


def test_interrupt_context_is_a_projection_attachment_not_message_mutation() -> None:
    message = UserMessage(content="request")
    state = SessionProjectionState(
        transcript_messages=[message],
        model_context_messages=[message],
        interrupted_run_by_id={
            "run-1": TurnInterruptedEvent("run-1", None, "user_interrupted", datetime.now(timezone.utc))
        },
    )

    reduce_session_event(state, TurnInterruptedContextAttachedEvent("run-1", message.id))

    assert state.transcript_messages[0].content == "request"
    assert state.model_context_messages[0].content == (f"request\n\n{TURN_ABORTED_FRAGMENT}")
    assert state.model_context_messages[0].id == message.id


@pytest.mark.asyncio
async def test_interrupted_pending_act_commits_cancel_result_and_terminal_facts_together(
    tmp_path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    call = UserMessage(content="request")
    log.commit_offline(MessageEvent(call))
    frontier = PendingActFrontier(
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
    arguments = {}
    log.commit_offline(PendingActCreatedEvent(frontier))
    log.commit_offline(
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(
                frontier.actions[0].invocation_id,
                0,
                arguments,
                tool_arguments_digest(arguments),
            ),
            None,
        )
    )
    log.commit_offline(
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False))
    )
    interrupted_at = datetime.now(timezone.utc)
    log.commit_offline(TurnInterruptedEvent("run-1", "call-1", "user_interrupted", interrupted_at))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    result = ToolMessage(content="cancelled", tool_call_id="action-1")

    from mote.contracts.execution.interrupt import RunInterruptPermit

    await RunInterruptService(projection, sink).settle(
        RunInterruptPermit("run-1", "worker-1", "incarnation-1", 1, interrupted_at),
        anchor_message_id=call.id,
        expected_stream_version=6,
        writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        result_events=(MessageAppendedEvent(result),),
        action_results=(
            PendingActionResultCommittedEvent(frontier.frontier_id, frontier.actions[0].invocation_id, result.id),
        ),
    )

    assert [type(event).__name__ for event in sink.batches[0].events] == [
        "MessageAppendedEvent",
        "PendingActionResultCommittedEvent",
        "PendingActInterruptedEvent",
        "RunRecoveryCursorAdvancedEvent",
        "TurnInterruptedContextAttachedEvent",
        "TurnInterruptSettledEvent",
    ]


@pytest.mark.asyncio
async def test_interrupted_started_external_replays_as_in_doubt_result(
    tmp_path,
) -> None:
    from mote.contracts.events.pending_act import ExternalEffectStartedEvent
    from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationIdentity

    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    anchor = UserMessage(content="request")
    log.commit_offline(MessageEvent(anchor))
    action = PendingAction(
        0,
        ToolInvocationId("invocation-1"),
        "action-1",
        "Remote",
        "remote/v1",
        1,
        ToolEffect.EXTERNAL,
        0,
    )
    frontier = PendingActFrontier(
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
        (action,),
    )
    arguments = {}
    digest = tool_arguments_digest(arguments)
    identity = ToolInvocationIdentity(
        action.invocation_id,
        ToolAttemptOrdinal(1),
        action.definition_identity,
        action.catalog_generation,
        digest,
        "agent-1",
        "run-1",
    )
    for event in (
        PendingActCreatedEvent(frontier),
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(action.invocation_id, 0, arguments, digest),
            None,
        ),
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False)),
        ExternalEffectStartedEvent(frontier.frontier_id, identity, None, 0, 1),
        TurnInterruptedEvent("run-1", "call-1", "user_interrupted", datetime.now(timezone.utc)),
    ):
        log.commit_offline(event)
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    sink = _Sink()
    result = ToolMessage(content="[IN_DOUBT] remote outcome unknown", tool_call_id="action-1")

    from mote.contracts.execution.interrupt import RunInterruptPermit

    interrupted = projection.snapshot().interrupted_run_by_id["run-1"]
    await RunInterruptService(projection, sink).settle(
        RunInterruptPermit("run-1", "worker-1", "incarnation-1", 1, interrupted.interrupted_at),
        anchor_message_id=anchor.id,
        expected_stream_version=7,
        writer=StreamWriterFence("run-1", "worker-1", "incarnation-1", 1),
        result_events=(MessageAppendedEvent(result),),
        action_results=(PendingActionResultCommittedEvent(frontier.frontier_id, action.invocation_id, result.id),),
        in_doubt_external_invocations=(action.invocation_id,),
    )

    for event in sink.batches[0].events:
        persisted_event = MessageEvent(event.message) if isinstance(event, MessageAppendedEvent) else event
        reduce_session_event(projection._state, persisted_event)
    state = projection.snapshot()
    assert state.external_effect_by_invocation[action.invocation_id].state.value == "in_doubt"
    assert "run-1" in state.settled_interrupt_runs
