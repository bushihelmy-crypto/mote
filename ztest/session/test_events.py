#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.session.events`` — event schema + line serde.

Covers: each event serializes to a ``{type, ts, payload}`` line; message
payloads round-trip back through ``Message.load``; compaction carries the full
replacement history; parse_line is forgiving on blank/corrupt lines.
"""
from __future__ import annotations

import json

from mote.common.schema import AIMessage, UserMessage
from mote.session.events import (
    COMPACTED,
    MESSAGE,
    SCHEMA_VERSION,
    SESSION_META,
    TURN_CONTEXT,
    CompactedEvent,
    MessageEvent,
    SessionMetaEvent,
    TurnContextEvent,
    parse_line,
    to_line,
)


def _roundtrip(event):
    line = to_line(event)
    record = parse_line(line)
    assert record is not None
    assert record["type"] == event.type
    assert "ts" in record
    return record


def test_session_meta_event_line():
    ev = SessionMetaEvent(session_id="abc", working_dir="/w", project_root="/p", model="gpt-4")
    record = _roundtrip(ev)
    assert record["type"] == SESSION_META
    assert record["payload"]["session_id"] == "abc"
    assert record["payload"]["schema_version"] == SCHEMA_VERSION
    assert record["payload"]["working_dir"] == "/w"


def test_message_event_roundtrips_through_message_load():
    from mote.common.schema import Message

    msg = UserMessage(content="hello world")
    record = _roundtrip(MessageEvent(message=msg))
    assert record["type"] == MESSAGE
    # The payload must reconstruct an equivalent Message.
    restored = Message.load(json.dumps(record["payload"]))
    assert restored is not None
    assert restored.content == "hello world"
    assert restored.id == msg.id


def test_compacted_event_carries_full_replacement_history():
    msgs = [UserMessage(content="summary placeholder"), AIMessage(content="tail")]
    record = _roundtrip(CompactedEvent(messages=msgs, summary="a summary"))
    assert record["type"] == COMPACTED
    assert record["payload"]["summary"] == "a summary"
    assert len(record["payload"]["replacement_history"]) == 2


def test_turn_context_event_line():
    record = _roundtrip(TurnContextEvent(turn_id="t1", working_dir="/w", model="m"))
    assert record["type"] == TURN_CONTEXT
    assert record["payload"]["turn_id"] == "t1"


def test_parse_line_is_forgiving():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("not json") is None
    assert parse_line(json.dumps({"no": "type"})) is None
    assert parse_line(json.dumps({"type": "x", "payload": {}})) == {"type": "x", "payload": {}}


def test_terminal_state_event_roundtrips():
    from mote.session.events import TERMINAL_STATE, TerminalStateEvent, parse_event

    ev = TerminalStateEvent(cwd="/tmp/work", env={"FOO": "bar", "BAZ": "qux"}, unset=["OLD"], tool="Terminal")
    record = _roundtrip(ev)
    assert record["type"] == TERMINAL_STATE
    assert record["payload"]["cwd"] == "/tmp/work"
    assert record["payload"]["env"] == {"FOO": "bar", "BAZ": "qux"}
    assert record["payload"]["unset"] == ["OLD"]

    rebuilt = parse_event(record)
    assert isinstance(rebuilt, TerminalStateEvent)
    assert rebuilt.cwd == "/tmp/work"
    assert rebuilt.env == {"FOO": "bar", "BAZ": "qux"}
    assert rebuilt.unset == ["OLD"]
    assert rebuilt.tool == "Terminal"


def test_kernel_state_event_roundtrips():
    from mote.session.events import KERNEL_STATE, KernelStateEvent, parse_event

    ev = KernelStateEvent(cwd="/tmp/work", env={"FOO": "bar", "BAZ": "qux"}, unset=["OLD"], tool="Jupyter")
    record = _roundtrip(ev)
    assert record["type"] == KERNEL_STATE
    assert record["payload"]["cwd"] == "/tmp/work"
    assert record["payload"]["env"] == {"FOO": "bar", "BAZ": "qux"}
    assert record["payload"]["unset"] == ["OLD"]

    rebuilt = parse_event(record)
    assert isinstance(rebuilt, KernelStateEvent)
    assert rebuilt.cwd == "/tmp/work"
    assert rebuilt.env == {"FOO": "bar", "BAZ": "qux"}
    assert rebuilt.unset == ["OLD"]
    assert rebuilt.tool == "Jupyter"
