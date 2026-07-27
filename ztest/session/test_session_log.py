#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.session.log.SessionLog`` — append-only JSONL.

Covers: create writes the session_meta first line and creates the directory;
create no-ops on an existing log (no double meta, no truncation); append is
O_APPEND (earlier lines survive); iter_raw skips corrupt lines.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.ports.event_journal import JournalIntegrityError
from mote.contracts.schema import UserMessage
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.log import ROLLOUT_FILENAME, SessionLog


def _log(tmp_path, session_id="sess1"):
    return SessionLog(session_id, base_dir=str(tmp_path))


def _append(log: SessionLog, event) -> None:
    asyncio.run(log.append(event))


def _events(log: SessionLog):
    return [decode_session_event(envelope) for envelope in log.iter_events()]


def test_create_writes_meta_first_line(tmp_path):
    log = _log(tmp_path)
    assert not log.exists()
    result = asyncio.run(log.append(SessionMetaEvent(session_id="sess1", working_dir="/w")))
    assert result.current_version == 1
    assert log.exists()
    assert log.path.name == ROLLOUT_FILENAME
    events = _events(log)
    assert len(events) == 1
    assert isinstance(events[0], SessionMetaEvent)
    assert events[0].session_id == "sess1"


def test_metadata_is_an_ordinary_fact_and_duplicate_append_is_visible(tmp_path):
    log = _log(tmp_path)
    _append(log, SessionMetaEvent(session_id="sess1"))
    _append(log, MessageEvent(message=UserMessage(content="hi")))

    events = _events(log)

    assert [event.type for event in events] == ["session_meta", "message"]


def test_stream_requires_exactly_one_matching_metadata_fact(tmp_path):
    log = _log(tmp_path)

    with pytest.raises(ValueError, match="first session fact"):
        _append(log, MessageEvent(message=UserMessage(content="orphan")))
    with pytest.raises(ValueError, match="identity"):
        _append(log, SessionMetaEvent(session_id="another"))

    _append(log, SessionMetaEvent(session_id="sess1"))
    with pytest.raises(ValueError, match="only be appended once"):
        _append(log, SessionMetaEvent(session_id="sess1"))


def test_append_is_append_only(tmp_path):
    log = _log(tmp_path)
    _append(log, SessionMetaEvent(session_id="sess1"))
    _append(log, MessageEvent(message=UserMessage(content="first")))
    _append(log, MessageEvent(message=UserMessage(content="second")))
    events = _events(log)
    assert [event.type for event in events] == ["session_meta", "message", "message"]
    assert events[1].message.content == "first"
    assert events[2].message.content == "second"


def test_verified_read_rejects_corrupt_lines(tmp_path):
    log = _log(tmp_path)
    _append(log, SessionMetaEvent(session_id="sess1"))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")

    with pytest.raises(JournalIntegrityError):
        list(log.iter_events())


def test_iter_raw_on_missing_log_is_empty(tmp_path):
    log = _log(tmp_path, session_id="never_created")
    assert list(log.iter_events()) == []
