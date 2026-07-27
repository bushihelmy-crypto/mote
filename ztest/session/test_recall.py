#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ledger recall — :func:`session.recall.body_for_tool_call`.

The rollout persists a ``MessageEvent`` with the *original* body the instant a
message is added. Recall fetches that body back by ``tool_call_id`` even after
the live history folded or pair-deleted it, so an inverse tool / memory recall
can restore it without re-running the tool.
"""
from __future__ import annotations

import asyncio

from mote.contracts.schema import AIMessage, ToolMessage, UserMessage
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.recall import body_for_tool_call


def _log(tmp_path) -> SessionLog:
    log = SessionLog("recall_sess", base_dir=str(tmp_path))
    _append(log, SessionMetaEvent(session_id="recall_sess"))
    return log


def _append(log: SessionLog, event) -> None:
    asyncio.run(log.append(event))


def _append_result(log: SessionLog, call_id: str, body: str) -> None:
    _append(log, MessageEvent(message=ToolMessage(content=body, tool_call_id=call_id)))


def test_returns_none_for_empty_id(tmp_path):
    log = _log(tmp_path)
    assert body_for_tool_call(log, "") is None


def test_returns_none_when_absent(tmp_path):
    log = _log(tmp_path)
    _append_result(log, "c1", "hello")
    assert body_for_tool_call(log, "nope") is None


def test_recovers_original_body(tmp_path):
    log = _log(tmp_path)
    _append_result(log, "c1", "the full original result body")

    msg = body_for_tool_call(log, "c1")
    assert msg is not None
    assert msg.content == "the full original result body"
    assert msg.is_tool_message()


def test_recovers_body_after_live_fold_left_rollout_intact(tmp_path):
    # The rollout line is never rewritten: even though the live history would
    # later fold this body to a placeholder, the recorded MessageEvent still
    # carries the original text.
    log = _log(tmp_path)
    _append_result(log, "c1", "PRE-FOLD full body")
    # Interleave unrelated traffic to prove the scan finds the right record.
    _append(log, MessageEvent(message=UserMessage(content="a user turn")))
    _append(log, MessageEvent(message=AIMessage(content="an assistant turn")))

    msg = body_for_tool_call(log, "c1")
    assert msg is not None
    assert msg.content == "PRE-FOLD full body"


def test_last_write_wins_for_re_added_id(tmp_path):
    log = _log(tmp_path)
    _append_result(log, "c1", "first recording")
    _append_result(log, "c1", "second recording")
    assert body_for_tool_call(log, "c1").content == "second recording"


def test_only_matches_tool_result_not_plain_messages(tmp_path):
    log = _log(tmp_path)
    _append(
        log,
        MessageEvent(message=UserMessage(content="c1")),
    )  # content equals the id, but no metadata
    assert body_for_tool_call(log, "c1") is None
