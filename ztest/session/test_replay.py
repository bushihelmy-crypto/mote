#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.roles.session.replay`` — rebuild history from a rollout.

Covers: plain message stream replays in order; a ``compacted`` checkpoint resets
the history to its self-contained ``replacement_history``; messages after a
checkpoint append onto it; meta is captured; corrupt/unloadable payloads are
skipped (counted) without aborting; turn_context is ignored for history.
"""
from __future__ import annotations

from metagpt.common.schema import AIMessage, UserMessage
from metagpt.roles.session.events import (
    CompactedEvent,
    MessageEvent,
    SessionMetaEvent,
    TurnContextEvent,
)
from metagpt.roles.session.log import SessionLog
from metagpt.roles.session.replay import replay


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
            messages=[UserMessage(content="[summary of olds]"), AIMessage(content="tail")],
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


def test_turn_context_ignored(tmp_path):
    log = _fresh_log(tmp_path)
    log.append(MessageEvent(message=UserMessage(content="hi")))
    log.append(TurnContextEvent(turn_id="t1", working_dir="/w"))
    result = replay(log)
    assert [m.content for m in result.messages] == ["hi"]


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
