from __future__ import annotations

import hashlib

import pytest

from mote.contracts.events.pending_act import (
    ExternalEffectStartedEvent,
    PendingActCreatedEvent,
    PendingActionArgumentsRevisedEvent,
    RunRecoveryCursorAdvancedEvent,
)
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.restore import InDoubtExecution, PendingActExecution
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.ports.execution.reconciliation import ReconciledExternalEffect
from mote.contracts.tool import (
    ToolAttemptOrdinal,
    ToolEffect,
    ToolEffectReceipt,
    ToolInvocationId,
    ToolInvocationIdentity,
    tool_arguments_digest,
)
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.execution_reconciliation import RuntimeExecutionReconciler
from mote.runtime.session.log import SessionLog
from mote.runtime.session.pending_act import RuntimePendingActService
from mote.runtime.session.projection import SessionLiveProjection


class _Sink:
    def __init__(self, projection):
        self.projection = projection
        self.batch = None

    async def commit_guarded(self, batch):
        self.batch = batch
        for event in batch.events:
            from mote.runtime.session.projection import reduce_session_event

            reduce_session_event(
                self.projection._state,
                (MessageEvent(event.message) if type(event).__name__ == "MessageAppendedEvent" else event),
            )
            self.projection._state.through_sequence += 1


class _UnknownQuery:
    def __init__(self):
        self.identities = []

    async def query_external_effect_result(self, identity, tool_name):
        assert tool_name == "External"
        self.identities.append(identity)
        return None


class _ResultQuery:
    def __init__(self, result):
        self.result = result

    async def query_external_effect_result(self, identity, tool_name):
        assert tool_name == "External"
        assert identity == self.result.receipt.identity
        return self.result


def _started_projection(tmp_path):
    log = SessionLog("session-1", base_dir=str(tmp_path))
    action = PendingAction(
        0,
        ToolInvocationId("invocation-1"),
        "action-1",
        "External",
        "external/v1",
        1,
        ToolEffect.EXTERNAL,
        0,
    )
    frontier = PendingActFrontier(
        1,
        PendingActFrontierId("frontier-1"),
        "session-1",
        "run-1",
        "model-1",
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
        (action,),
    )
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
    return projection, frontier, identity


@pytest.mark.asyncio
async def test_started_external_reconciliation_queries_only_and_fails_closed(tmp_path):
    projection, _frontier, identity = _started_projection(tmp_path)
    sink = _Sink(projection)
    query = _UnknownQuery()
    reconciler = RuntimeExecutionReconciler(projection, RuntimePendingActService(projection, sink), query)

    restored = await reconciler.reconcile_started_external_effects(
        "run-1", writer=StreamWriterFence("run-1", "worker", "incarnation", 1)
    )

    assert query.identities == [identity]
    assert isinstance(restored, InDoubtExecution)
    assert [type(event).__name__ for event in sink.batch.events] == ["ExternalEffectInDoubtEvent"]


@pytest.mark.asyncio
async def test_started_external_receipt_commits_result_atomically_without_dispatch(
    tmp_path,
):
    projection, frontier, identity = _started_projection(tmp_path)
    output = "recovered provider result"
    digest = f"sha256-{hashlib.sha256(output.encode()).hexdigest()}"
    receipt = ToolEffectReceipt("receipt-1", identity, "succeeded", {"provider": "done"}, (), (), (), digest)
    sink = _Sink(projection)
    reconciler = RuntimeExecutionReconciler(
        projection,
        RuntimePendingActService(projection, sink),
        _ResultQuery(ReconciledExternalEffect(receipt, output)),
    )

    restored = await reconciler.reconcile_started_external_effects(
        "run-1", writer=StreamWriterFence("run-1", "worker", "incarnation", 1)
    )

    assert isinstance(restored, PendingActExecution)
    assert [type(event).__name__ for event in sink.batch.events] == [
        "ExternalEffectFinishedEvent",
        "MessageAppendedEvent",
        "PendingActionResultCommittedEvent",
    ]
    state = projection.snapshot()
    result = state.pending_action_result_by_invocation[frontier.actions[0].invocation_id]
    assert result.receipt_id == receipt.receipt_id
    assert result.presentation_digest == digest
