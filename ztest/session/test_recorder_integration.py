#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ContextManager -> EventBus -> RecorderSubscriber wiring.

The recorder is no longer injected into ContextManager; it subscribes to the
shared event bus. ContextManager emits a ``MessageAppendedEvent`` per ``add``
(and per element of ``add_batch``) and a ``CompactionCheckpointEvent`` when
``manage_history`` rebuilds the history. The subscriber maps those to
``session/events.py`` records appended to a :class:`SessionLog`.

A spy subscriber isolates the wiring from disk; a real RecorderSubscriber over a
temp SessionLog confirms the end-to-end append. The compaction branch is driven
by monkeypatching ``autocompact`` so no LLM/threshold math is needed.
"""
from __future__ import annotations

import pytest

from metagpt.common.events import (
    CompactionCheckpointEvent,
    EventBus,
    MessageAppendedEvent,
)
from metagpt.common.schema import AIMessage, AutocompactResult, MicrocompactResult, UserMessage
from metagpt.context.manager import ContextManager
from metagpt.session.events import COMPACTED, MESSAGE
from metagpt.session.log import SessionLog
from metagpt.session.subscribers import RecorderSubscriber


class SpySubscriber:
    """An EventSubscriber that records the message/compaction events it sees."""

    priority = 80

    def __init__(self):
        self.messages = []
        self.compactions = []

    async def handle(self, event):
        if isinstance(event, MessageAppendedEvent):
            if event.message is not None:
                self.messages.append(event.message)
        elif isinstance(event, CompactionCheckpointEvent):
            self.compactions.append((list(event.messages), event.summary))
        return None


def _bus_with(sub) -> EventBus:
    bus = EventBus()
    bus.subscribe(sub)
    return bus


@pytest.mark.asyncio
async def test_add_streams_each_message_to_subscriber():
    spy = SpySubscriber()
    cm = ContextManager(bus=_bus_with(spy))
    await cm.add(UserMessage(content="one"))
    await cm.add_batch([AIMessage(content="two"), UserMessage(content="three")])
    await cm.add(None)  # falsy -> skipped, no event
    assert [m.content for m in spy.messages] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_manage_history_emits_compaction(monkeypatch):
    spy = SpySubscriber()
    cm = ContextManager(llm=object(), bus=_bus_with(spy))  # llm just needs to be non-None
    await cm.add(UserMessage(content="old"))

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
    # ...and exactly one compaction checkpoint was emitted with the summary.
    assert len(spy.compactions) == 1
    recorded_msgs, summary = spy.compactions[0]
    assert summary == "a summary"
    assert [m.content for m in recorded_msgs] == ["[summary]", "tail"]


@pytest.mark.asyncio
async def test_real_recorder_appends_to_log(tmp_path):
    log = SessionLog("sess_int", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    cm = ContextManager(bus=_bus_with(recorder))
    await cm.add(UserMessage(content="persisted"))
    await recorder.handle(
        CompactionCheckpointEvent(messages=[UserMessage(content="[summary]")], summary="s")
    )
    types = [r["type"] for r in log.iter_raw()]
    assert types == [MESSAGE, COMPACTED]


@pytest.mark.asyncio
async def test_disabled_recorder_does_not_append(tmp_path):
    log = SessionLog("sess_off", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log, enabled=False)
    cm = ContextManager(bus=_bus_with(recorder))
    await cm.add(UserMessage(content="ignored"))
    await recorder.handle(
        CompactionCheckpointEvent(messages=[UserMessage(content="x")], summary="s")
    )
    assert list(log.iter_raw()) == []
