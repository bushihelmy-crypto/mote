#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.roles.session.log.SessionLog`` — append-only JSONL.

Covers: create writes the session_meta first line and creates the directory;
create no-ops on an existing log (no double meta, no truncation); append is
O_APPEND (earlier lines survive); iter_raw skips corrupt lines.
"""
from __future__ import annotations

from metagpt.common.schema import UserMessage
from metagpt.roles.session.events import MessageEvent, SessionMetaEvent
from metagpt.roles.session.log import ROLLOUT_FILENAME, SessionLog


def _log(tmp_path, session_id="sess1"):
    return SessionLog(session_id, base_dir=str(tmp_path))


def test_create_writes_meta_first_line(tmp_path):
    log = _log(tmp_path)
    assert not log.exists()
    created = log.create(SessionMetaEvent(session_id="sess1", working_dir="/w"))
    assert created is True
    assert log.exists()
    assert log.path.name == ROLLOUT_FILENAME
    records = list(log.iter_raw())
    assert len(records) == 1
    assert records[0]["type"] == "session_meta"
    assert records[0]["payload"]["session_id"] == "sess1"


def test_create_noops_when_log_exists(tmp_path):
    log = _log(tmp_path)
    log.create(SessionMetaEvent(session_id="sess1"))
    log.append(MessageEvent(message=UserMessage(content="hi")))
    # Second create must not re-write meta or truncate the existing log.
    created_again = log.create(SessionMetaEvent(session_id="sess1"))
    assert created_again is False
    records = list(log.iter_raw())
    assert [r["type"] for r in records] == ["session_meta", "message"]


def test_append_is_append_only(tmp_path):
    log = _log(tmp_path)
    log.create(SessionMetaEvent(session_id="sess1"))
    log.append(MessageEvent(message=UserMessage(content="first")))
    log.append(MessageEvent(message=UserMessage(content="second")))
    records = list(log.iter_raw())
    assert [r["type"] for r in records] == ["session_meta", "message", "message"]
    assert records[1]["payload"]["content"] == "first"
    assert records[2]["payload"]["content"] == "second"


def test_iter_raw_skips_corrupt_lines(tmp_path):
    log = _log(tmp_path)
    log.create(SessionMetaEvent(session_id="sess1"))
    # Inject a corrupt line directly.
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
    log.append(MessageEvent(message=UserMessage(content="ok")))
    records = list(log.iter_raw())
    assert [r["type"] for r in records] == ["session_meta", "message"]


def test_iter_raw_on_missing_log_is_empty(tmp_path):
    log = _log(tmp_path, session_id="never_created")
    assert list(log.iter_raw()) == []
