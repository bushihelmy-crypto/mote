#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Typed session event payloads round-trip through the v3 fact codec."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.conversation import AIMessage, UserMessage
from mote.contracts.events.envelope import EventEnvelope, StreamId, thaw_json
from mote.contracts.runtime import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
)
from mote.contracts.tool import CommandProtocol, ToolsetIdentity
from mote.runtime.session.codec import decode_session_event, encode_session_event, session_stream_id, stable_event_type
from mote.runtime.session.events import (
    CONTEXT_COMPACTED,
    HISTORY_EDITED,
    MESSAGE,
    RUNTIME_CHECKPOINT,
    RUNTIME_COMMIT,
    RUNTIME_PROJECTION_ACKNOWLEDGED,
    SCHEMA_VERSION,
    SESSION_META,
    TURN_CONTEXT,
    ContextCompactedFact,
    HistoryEditedFact,
    MessageEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    RoutingDecisionFact,
    RuntimeCheckpointEvent,
    RuntimeCommitEvent,
    RuntimeProjectionAcknowledgedEvent,
    SessionMetaEvent,
    TurnContextEvent,
)


def _roundtrip(event):
    occurred_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    fact = encode_session_event(
        event,
        session_id="session-events",
        occurred_at=occurred_at,
    )
    envelope = EventEnvelope(
        event_id=fact.event_id,
        event_type=fact.event_type,
        schema_version=fact.schema_version,
        stream_id=StreamId(session_stream_id("session-events")),
        sequence=1,
        occurred_at=fact.occurred_at,
        recorded_at=occurred_at,
        payload=fact.payload,
        session_id=fact.session_id,
    )
    assert envelope.event_type == stable_event_type(event.type)
    assert type(decode_session_event(envelope)) is type(event)
    payload = thaw_json(envelope.payload)
    assert isinstance(payload, dict)
    record = {"type": event.type, "payload": payload, "envelope": envelope}
    assert record["type"] == event.type
    return record


def _decode(record):
    return decode_session_event(record["envelope"])


def test_session_meta_event_line():
    identity = ToolsetIdentity("workspace", "2", CommandProtocol.NATIVE)
    ev = SessionMetaEvent(
        session_id="abc",
        role_class="mote.agent.role.v1",
        working_dir="/w",
        project_root="/p",
        model="gpt-4",
        toolset_manifest=(identity,),
    )
    record = _roundtrip(ev)
    assert record["type"] == SESSION_META
    assert record["payload"]["session_id"] == "abc"
    assert record["payload"]["schema_version"] == SCHEMA_VERSION
    assert record["payload"]["working_dir"] == "/w"
    assert record["payload"]["toolset_manifest"] == [{"id": "workspace", "version": "2", "protocol": "native"}]
    restored = SessionMetaEvent.from_payload(record["payload"])
    assert restored.toolset_manifest == (identity,)


def test_session_meta_requires_current_identity() -> None:
    with pytest.raises(KeyError):
        SessionMetaEvent.from_payload({"session_id": "legacy"})


def test_message_event_roundtrips_through_message_load():
    msg = UserMessage(content="hello world")
    record = _roundtrip(MessageEvent(message=msg))
    assert record["type"] == MESSAGE
    restored = _decode(record)
    assert isinstance(restored, MessageEvent)
    assert restored.message.content == "hello world"
    assert restored.message.id == msg.id


def test_context_compacted_fact_carries_projection_and_source_identity():
    msgs = [UserMessage(content="summary placeholder"), AIMessage(content="tail")]
    record = _roundtrip(
        ContextCompactedFact(
            model_context_messages=msgs,
            source_message_ids=["m1", "m2"],
            summary="a summary",
            strategy="summarize",
        )
    )
    assert record["type"] == CONTEXT_COMPACTED
    assert record["payload"]["summary"] == "a summary"
    assert len(record["payload"]["model_context"]) == 2
    assert record["payload"]["source_message_ids"] == ["m1", "m2"]


def test_history_edited_fact_carries_removal_operation():
    record = _roundtrip(
        HistoryEditedFact(
            removed_message_ids=["m1", "m2"],
            reason="delete",
        )
    )

    assert record["type"] == HISTORY_EDITED
    assert record["payload"] == {
        "clear_all": False,
        "reason": "delete",
        "removed_message_ids": ["m1", "m2"],
    }


def test_turn_context_event_line():
    record = _roundtrip(TurnContextEvent(turn_id="t1", working_dir="/w", model="m"))
    assert record["type"] == TURN_CONTEXT
    assert record["payload"]["turn_id"] == "t1"


def test_routing_decision_fact_round_trips_complete_state():
    event = RoutingDecisionFact(
        decision={
            "decision_id": "decision-1",
            "selected_route_id": "interactive.strong",
            "state_generation": 1,
        },
        state={
            "schema_version": 1,
            "generation": 1,
            "recent_decisions": [],
            "seed_floor": None,
            "control_hold": None,
        },
    )
    record = _roundtrip(event)
    rebuilt = _decode(record)
    assert rebuilt == event
    assert record["payload"]["state"]["generation"] == 1


def test_prompt_rejected_event_roundtrips_as_safe_audit_fact():
    from mote.runtime.session.events import PROMPT_REJECTED, PromptRejectedEvent

    event = PromptRejectedEvent(
        prompt="use <agent-vault:token>",
        reason="organization policy denied the prompt",
        terminate=True,
    )
    record = _roundtrip(event)
    rebuilt = _decode(record)

    assert record["type"] == PROMPT_REJECTED
    assert rebuilt == event


def test_runtime_checkpoint_event_roundtrips():
    checkpoint = RuntimeCheckpoint(
        runtime_id="terminal-1",
        kind="terminal",
        epoch=2,
        revision=7,
        codec="terminal-state+json@1",
        schema_version=1,
        payload_ref="inline-json:e30=",
        digest="sha256:test",
        sensitivity="private",
        fidelity=CheckpointFidelity.LOGICAL,
    )

    record = _roundtrip(RuntimeCheckpointEvent(checkpoint=checkpoint, reason="write-commit"))
    rebuilt = _decode(record)

    assert record["type"] == RUNTIME_CHECKPOINT
    assert isinstance(rebuilt, RuntimeCheckpointEvent)
    assert rebuilt.checkpoint == checkpoint
    assert rebuilt.reason == "write-commit"


def test_runtime_commit_fact_and_projection_ack_roundtrip():
    checkpoint = RuntimeCheckpoint(
        runtime_id="canvas-1",
        kind="canvas",
        epoch=2,
        revision=8,
        codec="canvas+json@1",
        schema_version=1,
        payload_ref="inline-json:e30=",
        fidelity=CheckpointFidelity.FULL,
    )
    intent = RuntimeProjectionIntent(
        intent_id="artifact-export",
        projector="canvas-artifact",
        schema_version=1,
        options=(("retention", "session"),),
    )
    event = RuntimeCommitEvent(
        RuntimeCommitFact(
            commit_id="canvas-1.2.8",
            checkpoint=checkpoint,
            projections=(intent,),
            reason="write-commit",
        )
    )

    record = _roundtrip(event)
    rebuilt = _decode(record)

    assert record["type"] == RUNTIME_COMMIT
    assert rebuilt == event

    ack_event = RuntimeProjectionAcknowledgedEvent(
        RuntimeProjectionAck(
            commit_id="canvas-1.2.8",
            intent_id="artifact-export",
        )
    )
    ack_record = _roundtrip(ack_event)

    assert ack_record["type"] == RUNTIME_PROJECTION_ACKNOWLEDGED
    assert _decode(ack_record) == ack_event


def test_output_lifecycle_events_roundtrip():
    events = [
        OutputCandidateReceivedEvent(
            candidate_id="c1",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            representation="native",
            raw={"count": "bad"},
        ),
        OutputValidationRejectedEvent(
            candidate_id="c1",
            contract_id="test.report@1",
            issues=[{"path": ["count"], "code": "int_parsing", "message": "bad"}],
            correction_attempt=1,
            corrections_remaining=1,
            correction_allowed=True,
        ),
        OutputAcceptedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            correction_attempts=1,
        ),
        OutputCommitStartedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
        ),
        OutputCommittedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            correction_attempts=1,
        ),
        OutputPublishedEvent(candidate_id="c2", contract_id="test.report@1"),
    ]

    rebuilt = [_decode(_roundtrip(event)) for event in events]

    assert [type(event) for event in rebuilt] == [type(event) for event in events]
    assert rebuilt[-2].value == {"count": 1}


def test_output_event_rejects_legacy_payload_shape():
    with pytest.raises(ValueError, match="fields must be exactly"):
        OutputCommittedEvent.from_payload(
            {
                "candidate_id": "legacy-candidate",
                "contract_id": "legacy.report@1",
                "schema_fingerprint": "sha",
                "value": {"count": 1},
                "future_field": "ignored",
            }
        )
