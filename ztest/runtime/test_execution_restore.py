from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.events.pending_act import (
    ExternalEffectInDoubtEvent,
    ExternalEffectStartedEvent,
    PendingActCreatedEvent,
    PendingActionArgumentsRevisedEvent,
    PendingActionResultCommittedEvent,
    PendingActSettledEvent,
    RunRecoveryCursorAdvancedEvent,
    TurnInterruptedEvent,
)
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.restore import (
    CommittedExecution,
    ExternalEffectReconciliationRequired,
    InDoubtExecution,
    ObserveExecution,
    PendingActExecution,
    UnrecoverablePreV1Execution,
)
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.output import CommittedOutput
from mote.contracts.tool import (
    ToolAttemptOrdinal,
    ToolEffect,
    ToolInvocationId,
    ToolInvocationIdentity,
    tool_arguments_digest,
)
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.execution_restore import RuntimeExecutionRestore
from mote.runtime.session.log import SessionLog
from mote.runtime.session.projection import SessionLiveProjection


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


class _CommittedOutputQuery:
    def __init__(self, result: CommittedExecution[str]) -> None:
        self._result = result

    def restored_committed_execution(self) -> CommittedExecution[str]:
        return self._result


def test_restore_returns_committed_output_from_the_injected_query(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    presentation = AIMessage(content="done")
    committed = CommittedOutput("candidate", "contract", "schema", "value")

    restored = RuntimeExecutionRestore(
        projection,
        run_id="run-1",
        committed_output=_CommittedOutputQuery(CommittedExecution(committed, presentation)),
    ).snapshot()

    assert isinstance(restored, CommittedExecution)
    assert restored.result is committed
    assert restored.presentation is presentation


def test_restore_rejects_committed_output_with_an_active_pending_act(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    frontier = _frontier()
    for event in (
        SessionMetaEvent("session-1", "test.Role", ()),
        PendingActCreatedEvent(frontier),
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False)),
    ):
        log.commit_offline(event)
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    committed = CommittedOutput("candidate", "contract", "schema", "value")

    with pytest.raises(ValueError, match="conflicts with an active PendingAct"):
        RuntimeExecutionRestore(
            projection,
            run_id="run-1",
            committed_output=_CommittedOutputQuery(CommittedExecution(committed, AIMessage(content="done"))),
        ).snapshot()


def test_restore_resolves_act_then_observe_from_one_projection_snapshot(
    tmp_path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    frontier = _frontier()
    arguments = {}
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    log.commit_offline(PendingActCreatedEvent(frontier))
    log.commit_offline(
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(
                ToolInvocationId("invocation-1"),
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
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    restore = RuntimeExecutionRestore(projection, run_id="run-1")

    assert isinstance(restore.snapshot(), PendingActExecution)

    result_message = AIMessage(content="result")
    log.commit_offline(MessageEvent(result_message))
    log.commit_offline(
        PendingActionResultCommittedEvent(
            frontier.frontier_id,
            frontier.actions[0].invocation_id,
            result_message.id,
        )
    )
    log.commit_offline(PendingActSettledEvent(frontier.frontier_id, 0))
    log.commit_offline(
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 1, RecoveryTarget.OBSERVE, None, True))
    )
    projection.restore(log.iter_events())
    observed = restore.snapshot()
    assert isinstance(observed, ObserveExecution)
    assert observed.cursor.continue_inference is True


def test_restore_blocks_started_external_effect_instead_of_reinvoking(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    original = _frontier()
    action = dataclasses.replace(original.actions[0], tool_name="External", effect=ToolEffect.EXTERNAL)
    frontier = dataclasses.replace(original, actions=(action,))
    arguments = {}
    digest = tool_arguments_digest(arguments)
    identity = ToolInvocationIdentity(
        action.invocation_id,
        ToolAttemptOrdinal(1),
        action.definition_identity,
        1,
        digest,
        "agent-1",
        "run-1",
    )
    for event in (
        SessionMetaEvent("session-1", "test.Role", ()),
        PendingActCreatedEvent(frontier),
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(action.invocation_id, 0, arguments, digest),
            None,
        ),
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False)),
        ExternalEffectStartedEvent(frontier.frontier_id, identity, None, 0, 1),
    ):
        log.commit_offline(event)
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())

    restored = RuntimeExecutionRestore(projection, run_id="run-1").snapshot()

    assert isinstance(restored, ExternalEffectReconciliationRequired)
    assert restored.invocation_ids == (action.invocation_id,)

    log.commit_offline(ExternalEffectInDoubtEvent(frontier.frontier_id, action.invocation_id, {"query": "unknown"}))
    projection.restore(log.iter_events())

    blocked = RuntimeExecutionRestore(projection, run_id="run-1").snapshot()

    assert isinstance(blocked, InDoubtExecution)
    assert blocked.invocation_ids == (action.invocation_id,)


def test_pre_v1_interrupted_run_is_typed_unrecoverable(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    log.commit_offline(TurnInterruptedEvent("legacy-run", None, "user_interrupted", datetime.now(timezone.utc)))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())

    restored = RuntimeExecutionRestore(projection, run_id="legacy-run").snapshot()

    assert isinstance(restored, UnrecoverablePreV1Execution)
    assert restored.code == "UNRECOVERABLE_PRE_V1_PENDING_ACT"
