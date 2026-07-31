#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for deterministic transcript and model-context session projections."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.conversation import AIMessage, UserMessage
from mote.contracts.events.envelope import EventId, EventType, StreamId
from mote.contracts.ports.events.journal import UncommittedFact
from mote.contracts.runtime import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
    RuntimeProjectionRequest,
)
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.session.codec import UnsupportedSessionEventError, session_stream_id
from mote.runtime.session.events import (
    ContextCompactedFact,
    HistoryEditedFact,
    MessageEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    RuntimeCheckpointEvent,
    RuntimeCommitEvent,
    RuntimeProjectionAcknowledgedEvent,
    SessionMetaEvent,
    TurnContextEvent,
)
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay


def _fresh_log(tmp_path, sid="r"):
    log = SessionLog(sid, base_dir=str(tmp_path))
    log.commit_offline(
        SessionMetaEvent(
            session_id=sid,
            role_class="mote.agent.role.v1",
            toolset_manifest=(),
            working_dir="/w",
            project_root="/p",
        )
    )
    return log


def test_replay_plain_history(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(MessageEvent(message=UserMessage(content="hi")))
    log.commit_offline(MessageEvent(message=AIMessage(content="hello")))
    result = replay(log)
    assert [m.content for m in result.transcript_messages] == ["hi", "hello"]
    assert [m.content for m in result.model_context_messages] == ["hi", "hello"]
    assert result.message_events == 2
    assert result.compactions == 0
    assert result.meta["session_id"] == "r"
    assert result.meta["working_dir"] == "/w"


def test_compaction_changes_model_context_without_overwriting_transcript(tmp_path):
    log = _fresh_log(tmp_path)
    old_1 = UserMessage(content="old-1")
    old_2 = UserMessage(content="old-2")
    log.commit_offline(MessageEvent(message=old_1))
    log.commit_offline(MessageEvent(message=old_2))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[
                UserMessage(content="[summary of olds]"),
                AIMessage(content="tail"),
            ],
            source_message_ids=[str(old_1.id), str(old_2.id)],
            summary="s",
            strategy="summarize",
        )
    )
    result = replay(log)
    assert [m.content for m in result.transcript_messages] == ["old-1", "old-2"]
    assert [m.content for m in result.model_context_messages] == [
        "[summary of olds]",
        "tail",
    ]
    assert result.compactions == 1


def test_messages_after_compaction_append_to_both_projections(tmp_path):
    log = _fresh_log(tmp_path)
    old = UserMessage(content="old")
    log.commit_offline(MessageEvent(message=old))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="[summary]")],
            source_message_ids=[str(old.id)],
            summary="s",
            strategy="summarize",
        )
    )
    log.commit_offline(MessageEvent(message=UserMessage(content="new-1")))
    log.commit_offline(MessageEvent(message=AIMessage(content="new-2")))
    result = replay(log)
    assert [m.content for m in result.transcript_messages] == [
        "old",
        "new-1",
        "new-2",
    ]
    assert [m.content for m in result.model_context_messages] == [
        "[summary]",
        "new-1",
        "new-2",
    ]


def test_latest_context_compaction_wins_only_for_model_projection(tmp_path):
    log = _fresh_log(tmp_path)
    first = UserMessage(content="first")
    mid = UserMessage(content="mid")
    cp1 = UserMessage(content="cp1")
    log.commit_offline(MessageEvent(message=first))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[cp1],
            source_message_ids=[str(first.id)],
            summary="1",
        )
    )
    log.commit_offline(MessageEvent(message=mid))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="cp2")],
            source_message_ids=[str(cp1.id), str(mid.id)],
            summary="2",
        )
    )
    log.commit_offline(MessageEvent(message=UserMessage(content="after")))
    result = replay(log)
    assert [m.content for m in result.transcript_messages] == [
        "first",
        "mid",
        "after",
    ]
    assert [m.content for m in result.model_context_messages] == ["cp2", "after"]
    assert result.compactions == 2


def test_history_delete_removes_ids_without_discarding_compacted_source_facts(
    tmp_path,
):
    log = _fresh_log(tmp_path)
    old = UserMessage(content="old")
    recent = UserMessage(content="recent")
    log.commit_offline(MessageEvent(message=old))
    log.commit_offline(MessageEvent(message=recent))
    summary = UserMessage(content="summary")
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[summary, recent],
            source_message_ids=[str(old.id), str(recent.id)],
            summary="s",
        )
    )
    log.commit_offline(
        HistoryEditedFact(
            removed_message_ids=[str(recent.id)],
            reason="delete",
        )
    )

    result = replay(log)

    assert [message.content for message in result.transcript_messages] == ["old"]
    assert [message.content for message in result.model_context_messages] == ["summary"]
    assert result.history_edits == 1


def test_history_clear_empties_both_projections_without_rewriting_prior_facts(
    tmp_path,
):
    log = _fresh_log(tmp_path)
    log.commit_offline(MessageEvent(message=UserMessage(content="old")))
    log.commit_offline(HistoryEditedFact(removed_message_ids=[], clear_all=True, reason="clear"))

    result = replay(log)

    assert result.transcript_messages == []
    assert result.model_context_messages == []


def test_output_state_survives_message_compaction(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(
        OutputCandidateReceivedEvent(candidate_id="c1", contract_id="test.report@1", raw={"count": "bad"})
    )
    log.commit_offline(
        OutputValidationRejectedEvent(
            candidate_id="c1",
            contract_id="test.report@1",
            correction_attempt=1,
            corrections_remaining=1,
            correction_allowed=True,
        )
    )
    original = UserMessage(content="original")
    log.commit_offline(MessageEvent(message=original))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="summary")],
            source_message_ids=[str(original.id)],
        )
    )
    log.commit_offline(OutputCandidateReceivedEvent(candidate_id="c2", contract_id="test.report@1", raw={"count": 1}))
    log.commit_offline(
        OutputAcceptedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            correction_attempts=1,
        )
    )
    log.commit_offline(OutputCommitStartedEvent(candidate_id="c2", contract_id="test.report@1"))
    log.commit_offline(
        OutputCommittedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            validator_provenance=[
                {
                    "name": "policy",
                    "version": "1",
                    "stage": "policy",
                    "effect": "pure",
                    "determinism": "deterministic",
                    "decision": "accept",
                }
            ],
            correction_attempts=1,
        )
    )
    log.commit_offline(OutputPublishedEvent(candidate_id="c2", contract_id="test.report@1"))

    result = replay(log)

    assert [message.content for message in result.transcript_messages] == ["original"]
    assert [message.content for message in result.model_context_messages] == ["summary"]
    assert result.output_state == {
        "status": "published",
        "candidate_id": "c2",
        "contract_id": "test.report@1",
        "schema_fingerprint": "sha",
        "value": {"count": 1},
        "correction_attempts": 1,
        "fencing_token": 0,
        "validator_provenance": [
            {
                "name": "policy",
                "version": "1",
                "stage": "policy",
                "effect": "pure",
                "determinism": "deterministic",
                "decision": "accept",
            }
        ],
    }


def test_unknown_event_rejects_replay(tmp_path):
    log = _fresh_log(tmp_path)
    stream_id = StreamId(session_stream_id(log.session_id))
    occurred_at = datetime.now(timezone.utc)
    LocalEventJournal(log.path, stream_id).append_committed(
        stream_id,
        (
            UncommittedFact(
                event_id=EventId("future-event"),
                event_type=EventType("mote.extension.future_event"),
                schema_version=1,
                occurred_at=occurred_at,
                payload={"value": 1},
            ),
        ),
        expected_version=1,
    )
    with pytest.raises(UnsupportedSessionEventError):
        replay(SessionLog(log.session_id, base_dir=str(tmp_path)))


def test_duplicate_output_fact_is_idempotent(tmp_path):
    log = _fresh_log(tmp_path)
    committed = OutputCommittedEvent(
        candidate_id="candidate-1",
        contract_id="test.report@1",
        schema_fingerprint="sha",
        value={"count": 1},
        run_id="run-1",
    )
    log.commit_offline(committed)
    log.commit_offline(committed)

    state = replay(log).output_states["run-1"]

    assert state["status"] == "committed"
    assert state["value"] == {"count": 1}
    assert state["candidate_id"] == "candidate-1"


def test_turn_context_ignored(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(MessageEvent(message=UserMessage(content="hi")))
    log.commit_offline(TurnContextEvent(turn_id="t1", working_dir="/w"))
    result = replay(log)
    assert [m.content for m in result.model_context_messages] == ["hi"]


def test_runtime_checkpoint_is_last_write_wins_per_readable_runtime(tmp_path):
    log = _fresh_log(tmp_path)
    first = RuntimeCheckpoint(
        runtime_id="terminal-old",
        kind="terminal",
        epoch=1,
        revision=1,
        codec="terminal@1",
        schema_version=1,
        payload_ref="memory:first",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    latest = RuntimeCheckpoint(
        runtime_id="terminal-new",
        kind="terminal",
        epoch=2,
        revision=3,
        codec="terminal@1",
        schema_version=1,
        payload_ref="memory:latest",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    secondary = RuntimeCheckpoint(
        runtime_id="terminal-secondary",
        kind="terminal",
        alias="secondary",
        epoch=1,
        revision=4,
        codec="terminal@1",
        schema_version=1,
        payload_ref="memory:secondary",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    log.commit_offline(RuntimeCheckpointEvent(first, reason="write-commit"))
    log.commit_offline(RuntimeCheckpointEvent(secondary, reason="write-commit"))
    log.commit_offline(RuntimeCheckpointEvent(latest, reason="handoff-after"))

    result = replay(log)

    assert result.runtime_checkpoints == {
        "terminal:default": latest,
        "terminal:secondary": secondary,
    }


def test_runtime_commit_replays_only_unacknowledged_projection_work(tmp_path):
    log = _fresh_log(tmp_path)
    checkpoint = RuntimeCheckpoint(
        runtime_id="canvas-1",
        kind="canvas",
        epoch=3,
        revision=9,
        codec="canvas+json@1",
        schema_version=1,
        payload_ref="memory:canvas",
        fidelity=CheckpointFidelity.FULL,
    )
    completed = RuntimeProjectionIntent(
        intent_id="svg",
        projector="canvas-artifact",
        schema_version=1,
    )
    pending = RuntimeProjectionIntent(
        intent_id="drawio",
        projector="canvas-artifact",
        schema_version=1,
    )
    fact = RuntimeCommitFact(
        commit_id="canvas-1.3.9",
        checkpoint=checkpoint,
        projections=(completed, pending),
        reason="write-commit",
    )
    log.commit_offline(RuntimeCommitEvent(fact))
    log.commit_offline(
        RuntimeProjectionAcknowledgedEvent(
            RuntimeProjectionAck(
                commit_id=fact.commit_id,
                intent_id=completed.intent_id,
            )
        )
    )
    # A retried append of the same commit fact must not resurrect acknowledged work.
    log.commit_offline(RuntimeCommitEvent(fact))

    result = replay(log)

    request = RuntimeProjectionRequest(
        commit_id=fact.commit_id,
        checkpoint=checkpoint,
        intent=pending,
    )
    assert result.runtime_checkpoints == {"canvas:default": checkpoint}
    assert result.pending_runtime_projections == {request.key: request}


def test_publication_outbox_survives_crash_before_ack(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(
        OutputCommittedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            validator_provenance=[
                {
                    "name": "policy",
                    "version": "1",
                    "stage": "policy",
                    "effect": "pure",
                    "determinism": "deterministic",
                    "decision": "accept",
                }
            ],
            run_id="run-1",
        )
    )
    log.commit_offline(
        OutputPublicationQueuedEvent(
            publication_id="pub-1",
            candidate_id="c2",
            contract_id="test.report@1",
            run_id="run-1",
        )
    )

    assert replay(log).output_state == {
        "status": "publication_queued",
        "candidate_id": "c2",
        "contract_id": "test.report@1",
        "schema_fingerprint": "sha",
        "value": {"count": 1},
        "correction_attempts": 0,
        "fencing_token": 0,
        "validator_provenance": [
            {
                "name": "policy",
                "version": "1",
                "stage": "policy",
                "effect": "pure",
                "determinism": "deterministic",
                "decision": "accept",
            }
        ],
        "publication_id": "pub-1",
        "run_id": "run-1",
        "run_kind": "agent",
    }
    assert replay(log).output_states["run-1"]["status"] == "publication_queued"


def test_interleaved_agent_and_graph_outputs_fold_by_run_id(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(
        OutputAcceptedEvent(
            candidate_id="agent-candidate",
            contract_id="mote.text@1",
            schema_fingerprint="agent-schema",
            value="agent result",
            run_id="agent-run",
            run_kind="agent",
        )
    )
    log.commit_offline(
        OutputAcceptedEvent(
            candidate_id="graph-candidate",
            contract_id="mote.graph-json@1",
            schema_fingerprint="graph-schema",
            value={"answer": 42},
            run_id="graph-run",
            run_kind="graph",
        )
    )
    log.commit_offline(
        OutputCommitStartedEvent(
            candidate_id="agent-candidate",
            contract_id="mote.text@1",
            run_id="agent-run",
            fencing_token=7,
            run_kind="agent",
        )
    )

    states = replay(log).output_states

    assert states["agent-run"]["status"] == "commit_started"
    assert states["agent-run"]["fencing_token"] == 7
    assert states["agent-run"]["value"] == "agent result"
    assert states["agent-run"]["run_kind"] == "agent"
    assert states["graph-run"]["status"] == "accepted"
    assert states["graph-run"]["value"] == {"answer": 42}
    assert states["graph-run"]["run_kind"] == "graph"


def test_migrated_output_replays_as_current_contract_acceptance(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(
        OutputMigratedEvent(
            candidate_id="candidate-1",
            source_contract_id="test.integer@1",
            target_contract_id="test.report@2",
            target_schema_fingerprint="report-fp",
            value={"count": 7},
            steps=[
                {
                    "name": "integer-to-report",
                    "version": "1",
                    "source_contract_id": "test.integer@1",
                    "target_contract_id": "test.report@2",
                }
            ],
            run_id="run-1",
            run_kind="agent",
        )
    )

    state = replay(log).output_states["run-1"]

    assert state["status"] == "accepted"
    assert state["contract_id"] == "test.report@2"
    assert state["value"] == {"count": 7}
    assert state["migration_provenance"][0]["name"] == "integer-to-report"


def test_corrupt_v3_line_is_rejected(tmp_path):
    log = _fresh_log(tmp_path)
    log.commit_offline(MessageEvent(message=UserMessage(content="good")))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("not json\n")

    reopened = SessionLog(log.session_id, base_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="integrity failure"):
        replay(reopened)


def test_replay_missing_log_is_empty(tmp_path):
    log = SessionLog("nope", base_dir=str(tmp_path))
    result = replay(log)
    assert result.transcript_messages == []
    assert result.model_context_messages == []
    assert result.meta is None
