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
by a fake summarizer LLM plus a forced-low threshold so a real summarize runs
through the ContextEngine (no monkeypatching of module functions).
"""
from __future__ import annotations

import pytest

import metagpt.context.budget as token_budget
from metagpt.common.events import (
    CompactionCheckpointEvent,
    EventBus,
    MessageAppendedEvent,
)
from metagpt.common.interface.event_subscriber import ObservationSubscriber
from metagpt.common.schema import AIMessage, ContextManagerConfig, UserMessage
from metagpt.context.manager import ContextManager


class _FakeLLM:
    def __init__(self, *, summary: str = "sum", model: str = "m"):
        self.model = model
        self._summary = summary

    async def aask(self, msg=None, system_msgs=None, stream=True, **kwargs) -> str:
        return self._summary
from metagpt.session.events import COMPACTED, MESSAGE
from metagpt.session.log import SessionLog
from metagpt.session.subscribers import RecorderSubscriber


class SpySubscriber(ObservationSubscriber):
    """An ObservationSubscriber that records the message/compaction events it sees."""

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
    monkeypatch.setattr(token_budget, "autocompact_threshold", lambda model: 1)
    spy = SpySubscriber()
    cfg = ContextManagerConfig(enable_microcompact=False, keep_tail_messages=1, keep_tail_tokens=1)
    cm = ContextManager(llm=_FakeLLM(summary="a summary"), config=cfg, model="m", bus=_bus_with(spy))
    for i in range(6):
        await cm.add(UserMessage(content=f"turn {i} content here"))

    changed = await cm.manage_history()
    assert changed is True
    # The backing history is swapped to the rebuilt [summary] + tail...
    assert any("a summary" in (m.content or "") for m in cm.messages)
    # ...and exactly one compaction checkpoint was emitted with the summary.
    assert len(spy.compactions) == 1
    recorded_msgs, summary = spy.compactions[0]
    assert summary == "a summary"
    # the checkpoint carries the full rebuilt history.
    assert [m.content for m in recorded_msgs] == [m.content for m in cm.messages]


@pytest.mark.asyncio
async def test_real_recorder_appends_to_log(tmp_path):
    log = SessionLog("sess_int", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    cm = ContextManager(bus=_bus_with(recorder))
    await cm.add(UserMessage(content="persisted"))
    await recorder.handle(
        CompactionCheckpointEvent(messages=[UserMessage(content="[summary]")], summary="s")
    )
    # iter_raw() drains the DiskWriter first, so queued writes are on disk here.
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


@pytest.mark.asyncio
async def test_llm_response_persists_compact_call_record(tmp_path):
    from metagpt.common.events import LLMResponseEvent
    from metagpt.session.events import LLM_CALL, parse_event

    log = SessionLog("sess_llm", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    await recorder.handle(
        LLMResponseEvent(
            request_id="rq1",
            model="gpt-4o",
            content="hello",
            usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            cost_usd=0.0012,
            latency_ms=33.0,
        )
    )
    raw = list(log.iter_raw())
    assert [r["type"] for r in raw] == [LLM_CALL]
    ev = parse_event(raw[0])
    assert ev.request_id == "rq1" and ev.model == "gpt-4o"
    assert ev.usage["input_tokens"] == 100
    assert ev.cost_usd == 0.0012
    # No prompt/completion text is persisted (it lands as message records).
    assert "content" not in raw[0]["payload"]


@pytest.mark.asyncio
async def test_llm_response_without_usage_is_not_recorded(tmp_path):
    from metagpt.common.events import LLMResponseEvent

    log = SessionLog("sess_llm_empty", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    await recorder.handle(LLMResponseEvent(request_id="rq2", model="gpt-4o", usage=None))
    assert list(log.iter_raw()) == []
