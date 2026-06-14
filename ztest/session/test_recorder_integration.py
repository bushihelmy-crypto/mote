#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ContextManager <-> SessionRecorder wiring.

The recorder is injected into ContextManager and must receive:
  * one ``record_message`` per ``add`` (and per element of ``add_batch``), and
  * one ``record_compaction`` when ``manage_history`` rebuilds the history.

A spy recorder isolates the wiring from disk; a real SessionRecorder over a
temp SessionLog confirms the end-to-end append. The compaction branch is driven
by monkeypatching ``autocompact`` so no LLM/threshold math is needed.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import AIMessage, AutocompactResult, MicrocompactResult, UserMessage
from metagpt.context.manager import ContextManager
from metagpt.session.events import COMPACTED, MESSAGE
from metagpt.session.log import SessionLog
from metagpt.session.recorder import SessionRecorder


class SpyRecorder:
    """Conforms to common.interface.SessionRecorder; records calls."""

    def __init__(self):
        self.messages = []
        self.compactions = []

    def record_message(self, message):
        self.messages.append(message)

    def record_compaction(self, messages, summary):
        self.compactions.append((list(messages), summary))


def test_add_streams_each_message_to_recorder():
    spy = SpyRecorder()
    cm = ContextManager(recorder=spy)
    cm.add(UserMessage(content="one"))
    cm.add_batch([AIMessage(content="two"), UserMessage(content="three")])
    cm.add(None)  # falsy -> skipped, no record
    assert [m.content for m in spy.messages] == ["one", "two", "three"]


def test_recorder_conforms_to_protocol():
    from metagpt.common.interface import SessionRecorder as SessionRecorderProto

    assert isinstance(SpyRecorder(), SessionRecorderProto)


@pytest.mark.asyncio
async def test_manage_history_records_compaction(monkeypatch):
    spy = SpyRecorder()
    cm = ContextManager(llm=object(), recorder=spy)  # llm just needs to be non-None
    cm.add(UserMessage(content="old"))

    rebuilt = [UserMessage(content="[summary]"), AIMessage(content="tail")]

    def fake_micro(messages, config, *, model, compactable):
        return MicrocompactResult(messages=messages, tokens_freed=0)

    async def fake_auto(messages, llm, config, *, model, tokens_freed, consecutive_failures, custom_instructions):
        return AutocompactResult(messages=rebuilt, compacted=True, summary="a summary")

    monkeypatch.setattr("metagpt.context.manager.microcompact", fake_micro)
    monkeypatch.setattr("metagpt.context.manager.autocompact", fake_auto)

    changed = await cm.manage_history()
    assert changed is True
    # The backing history is swapped to the rebuilt list...
    assert [m.content for m in cm.messages] == ["[summary]", "tail"]
    # ...and exactly one compaction checkpoint was recorded with the summary.
    assert len(spy.compactions) == 1
    recorded_msgs, summary = spy.compactions[0]
    assert summary == "a summary"
    assert [m.content for m in recorded_msgs] == ["[summary]", "tail"]


def test_real_recorder_appends_to_log(tmp_path):
    log = SessionLog("sess_int", base_dir=str(tmp_path))
    recorder = SessionRecorder(log)
    cm = ContextManager(recorder=recorder)
    cm.add(UserMessage(content="persisted"))
    recorder.record_compaction([UserMessage(content="[summary]")], "s")
    types = [r["type"] for r in log.iter_raw()]
    assert types == [MESSAGE, COMPACTED]


def test_disabled_recorder_does_not_append(tmp_path):
    log = SessionLog("sess_off", base_dir=str(tmp_path))
    recorder = SessionRecorder(log, enabled=False)
    cm = ContextManager(recorder=recorder)
    cm.add(UserMessage(content="ignored"))
    recorder.record_compaction([UserMessage(content="x")], "s")
    assert list(log.iter_raw()) == []
