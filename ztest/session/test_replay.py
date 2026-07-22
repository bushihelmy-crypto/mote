#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.session.replay`` — rebuild history from a rollout.

Covers: plain message stream replays in order; a ``compacted`` checkpoint resets
the history to its self-contained ``replacement_history``; messages after a
checkpoint append onto it; meta is captured; corrupt/unloadable payloads are
skipped (counted) without aborting; turn_context is ignored for history.
"""
from __future__ import annotations

from mote.common.schema import AIMessage, UserMessage
from mote.session.events import (
    CompactedEvent,
    KernelStateEvent,
    MessageEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    SessionMetaEvent,
    TerminalStateEvent,
    TurnContextEvent,
)
from mote.session.log import SessionLog
from mote.session.replay import replay


def _fresh_log(tmp_path, sid="r"):
    log = SessionLog(sid, base_dir=str(tmp_path))
    log.create(SessionMetaEvent(session_id=sid, working_dir="/w", project_root="/p"))
    return log


def test_replay_plain_history(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    log.append(MessageEvent(message=AIMessage(content="hello")))
    result = replay(log)
    assert [m.content for m in result.messages] == ["hi", "hello"]
    assert result.message_events == 2
    assert result.checkpoints == 0
    assert result.from_checkpoint is False
    assert result.meta["session_id"] == "r"
    assert result.meta["working_dir"] == "/w"


def test_compacted_resets_to_replacement_history(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="old-1")))
    log.append(MessageEvent(message=UserMessage(content="old-2")))
    # Compaction folds the two olds into a summary+tail checkpoint.
    log.append(
        CompactedEvent(
            messages=[
                UserMessage(content="[summary of olds]"),
                AIMessage(content="tail"),
            ],
            summary="s",
        )
    )
    result = replay(log)
    # Pre-checkpoint appends are discarded; history == replacement_history.
    assert [m.content for m in result.messages] == ["[summary of olds]", "tail"]
    assert result.from_checkpoint is True
    assert result.checkpoints == 1


def test_messages_after_checkpoint_append(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="old")))
    log.append(CompactedEvent(messages=[UserMessage(content="[summary]")], summary="s"))
    log.append(MessageEvent(message=UserMessage(content="new-1")))
    log.append(MessageEvent(message=AIMessage(content="new-2")))
    result = replay(log)
    assert [m.content for m in result.messages] == ["[summary]", "new-1", "new-2"]


def test_latest_checkpoint_wins(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(CompactedEvent(messages=[UserMessage(content="cp1")], summary="1"))
    log.append(MessageEvent(message=UserMessage(content="mid")))
    log.append(CompactedEvent(messages=[UserMessage(content="cp2")], summary="2"))
    log.append(MessageEvent(message=UserMessage(content="after")))
    result = replay(log)
    assert [m.content for m in result.messages] == ["cp2", "after"]
    assert result.checkpoints == 2


def test_output_state_survives_message_compaction(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(OutputCandidateReceivedEvent(candidate_id="c1", contract_id="test.report@1", raw={"count": "bad"}))
    log.append(
        OutputValidationRejectedEvent(
            candidate_id="c1",
            contract_id="test.report@1",
            correction_attempt=1,
            corrections_remaining=1,
            correction_allowed=True,
        )
    )
    log.append(CompactedEvent(messages=[UserMessage(content="summary")]))
    log.append(OutputCandidateReceivedEvent(candidate_id="c2", contract_id="test.report@1", raw={"count": 1}))
    log.append(
        OutputAcceptedEvent(
            candidate_id="c2",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 1},
            correction_attempts=1,
        )
    )
    log.append(OutputCommitStartedEvent(candidate_id="c2", contract_id="test.report@1"))
    log.append(
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
    log.append(OutputPublishedEvent(candidate_id="c2", contract_id="test.report@1"))

    result = replay(log)

    assert [message.content for message in result.messages] == ["summary"]
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


def test_turn_context_ignored(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    log.append(TurnContextEvent(turn_id="t1", working_dir="/w"))
    result = replay(log)
    assert [m.content for m in result.messages] == ["hi"]


def test_publication_outbox_survives_crash_before_ack(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(
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
    log.append(
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
    log.append(
        OutputAcceptedEvent(
            candidate_id="agent-candidate",
            contract_id="mote.text@1",
            schema_fingerprint="agent-schema",
            value="agent result",
            run_id="agent-run",
            run_kind="agent",
        )
    )
    log.append(
        OutputAcceptedEvent(
            candidate_id="graph-candidate",
            contract_id="mote.graph-json@1",
            schema_fingerprint="graph-schema",
            value={"answer": 42},
            run_id="graph-run",
            run_kind="graph",
        )
    )
    log.append(
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
    log.append(
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


def test_corrupt_lines_skipped(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="good")))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("not json\n")
    log.append(MessageEvent(message=AIMessage(content="also good")))
    result = replay(log)
    assert [m.content for m in result.messages] == ["good", "also good"]


def test_replay_missing_log_is_empty(tmp_path):
    log = SessionLog("nope", base_dir=str(tmp_path))
    result = replay(log)
    assert result.messages == []
    assert result.meta is None


def test_terminal_state_none_by_default(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    result = replay(log)
    assert result.terminal_state is None


def test_terminal_state_captured(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    log.append(TerminalStateEvent(cwd="/tmp", env={"FOO": "bar"}, unset=["OLD"], tool="Terminal"))
    result = replay(log)
    # Terminal state is not part of the message-history rebuild.
    assert [m.content for m in result.messages] == ["hi"]
    assert result.terminal_state == {
        "cwd": "/tmp",
        "env": {"FOO": "bar"},
        "unset": ["OLD"],
    }


def test_terminal_state_last_write_wins(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(TerminalStateEvent(cwd="/first", env={"A": "1"}, unset=[]))
    log.append(TerminalStateEvent(cwd="/second", env={"B": "2"}, unset=["A"]))
    result = replay(log)
    assert result.terminal_state == {
        "cwd": "/second",
        "env": {"B": "2"},
        "unset": ["A"],
    }


def test_kernel_state_none_by_default(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    result = replay(log)
    assert result.kernel_state is None


def test_kernel_state_captured(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    log.append(KernelStateEvent(cwd="/tmp", env={"FOO": "bar"}, unset=["OLD"], tool="Jupyter"))
    result = replay(log)
    # Kernel state is not part of the message-history rebuild.
    assert [m.content for m in result.messages] == ["hi"]
    assert result.kernel_state == {
        "cwd": "/tmp",
        "env": {"FOO": "bar"},
        "unset": ["OLD"],
    }


def test_kernel_state_last_write_wins(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(KernelStateEvent(cwd="/first", env={"A": "1"}, unset=[]))
    log.append(KernelStateEvent(cwd="/second", env={"B": "2"}, unset=["A"]))
    result = replay(log)
    assert result.kernel_state == {"cwd": "/second", "env": {"B": "2"}, "unset": ["A"]}


def test_terminal_and_kernel_states_are_independent(tmp_path):
    """Both restore on resume without clobbering each other (the split's point)."""
    log = _fresh_log(tmp_path)
    log.append(TerminalStateEvent(cwd="/shell", env={"SH": "1"}, unset=[]))
    log.append(KernelStateEvent(cwd="/kernel", env={"KE": "2"}, unset=[]))
    result = replay(log)
    assert result.terminal_state == {"cwd": "/shell", "env": {"SH": "1"}, "unset": []}
    assert result.kernel_state == {"cwd": "/kernel", "env": {"KE": "2"}, "unset": []}
