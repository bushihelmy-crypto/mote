from __future__ import annotations

import dataclasses

import pytest

from mote.contracts.events.pending_act import (
    ApprovalDecisionCommittedEvent,
    ApprovalRequestedEvent,
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
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.interaction import ApprovalDisposition, ApprovalRequest, ApprovalRequestId, ApprovalState
from mote.contracts.tool import (
    ToolAttemptOrdinal,
    ToolEffect,
    ToolInvocationId,
    ToolInvocationIdentity,
    tool_arguments_digest,
)
from mote.runtime.session.codec import decode_session_event, encode_session_event
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.projection import SessionProjectionState, reduce_session_envelope


def _definition() -> ToolCompositionDefinitionRef:
    return ToolCompositionDefinitionRef(
        "agent",
        "1",
        "sha256-executable",
        "generation-1",
        "sha256-catalog",
        "sha256-provider",
        "policy-1",
        "sha256-capability",
    )


def _frontier() -> PendingActFrontier:
    return PendingActFrontier(
        1,
        PendingActFrontierId("frontier-1"),
        "session-1",
        "run-1",
        "model-call-1",
        0,
        _definition(),
        (
            PendingAction(
                0, ToolInvocationId("invocation-1"), "action-1", "External", "external/v1", 1, ToolEffect.EXTERNAL, 0
            ),
        ),
    )


def _request(digest: str) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="External",
        request_id=ApprovalRequestId("approval-1"),
        frontier_id=PendingActFrontierId("frontier-1"),
        invocation_id=ToolInvocationId("invocation-1"),
        arguments_revision=0,
        arguments_digest=digest,
        permission_targets_digest="sha256-targets",
        expected_frontier_revision=0,
    )


def _log(tmp_path) -> SessionLog:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    return log


def test_pending_act_events_strict_round_trip() -> None:
    event = PendingActCreatedEvent(_frontier())
    fact = encode_session_event(event, session_id="session-1")
    from datetime import datetime, timezone

    from mote.contracts.events.envelope import EventEnvelope, StreamId

    envelope = EventEnvelope(
        fact.event_id,
        fact.event_type,
        fact.schema_version,
        StreamId("session/session-1"),
        1,
        fact.occurred_at,
        datetime.now(timezone.utc),
        fact.payload,
        session_id="session-1",
        run_id="run-1",
    )
    assert decode_session_event(envelope) == event
    payload = dict(fact.payload)
    payload["unknown"] = True
    with pytest.raises(ValueError, match="canonical"):
        PendingActCreatedEvent.from_payload(payload)


def test_projection_separates_approval_and_external_execution(tmp_path) -> None:
    log = _log(tmp_path)
    frontier = _frontier()
    arguments = {"value": 1}
    digest = tool_arguments_digest(arguments)
    request = _request(digest)
    identity = ToolInvocationIdentity(
        ToolInvocationId("invocation-1"), ToolAttemptOrdinal(1), "external/v1", 1, digest, "agent-1", "run-1"
    )
    events = (
        PendingActCreatedEvent(frontier),
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id, PendingActionArgumentsRevision(identity.invocation_id, 0, arguments, digest), None
        ),
        RunRecoveryCursorAdvancedEvent(RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, frontier.frontier_id, False)),
        ApprovalRequestedEvent(request),
        ApprovalDecisionCommittedEvent(request.request_id, ApprovalDisposition.ALLOW_ONCE, 0, digest),
    )
    for event in events:
        log.commit_offline(event)
    state = SessionProjectionState()
    for envelope in log.iter_events():
        reduce_session_envelope(state, envelope)

    assert state.approval_by_request_id[request.request_id].state is ApprovalState.APPROVED
    assert identity.invocation_id not in state.external_effect_by_invocation
    log.commit_offline(ExternalEffectStartedEvent(frontier.frontier_id, identity, request.request_id, 0, 1))
    reduce_session_envelope(state, tuple(log.iter_events())[-1])
    assert state.approval_by_request_id[request.request_id].state is ApprovalState.APPROVED
    assert state.external_effect_by_invocation[identity.invocation_id].state.value == "started"
    assert "state" not in {field.name for field in dataclasses.fields(PendingAction)}


def test_old_approval_cannot_authorize_new_argument_revision(tmp_path) -> None:
    log = _log(tmp_path)
    frontier = _frontier()
    original = {"value": 1}
    changed = {"value": 2}
    original_digest = tool_arguments_digest(original)
    changed_digest = tool_arguments_digest(changed)
    for event in (
        PendingActCreatedEvent(frontier),
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(ToolInvocationId("invocation-1"), 0, original, original_digest),
            None,
        ),
        ApprovalRequestedEvent(_request(original_digest)),
        PendingActionArgumentsRevisedEvent(
            frontier.frontier_id,
            PendingActionArgumentsRevision(ToolInvocationId("invocation-1"), 1, changed, changed_digest),
            original_digest,
        ),
    ):
        log.commit_offline(event)
    state = SessionProjectionState()
    for envelope in log.iter_events():
        reduce_session_envelope(state, envelope)

    with pytest.raises(ValueError, match="arguments do not match"):
        from mote.runtime.session.projection import reduce_session_event

        reduce_session_event(
            state,
            ApprovalDecisionCommittedEvent(
                ApprovalRequestId("approval-1"), ApprovalDisposition.ALLOW_ONCE, 1, changed_digest
            ),
        )
