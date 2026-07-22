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

import mote.context.budget as token_budget
from mote.common.events import CompactionCheckpointEvent, EventBus, MessageAppendedEvent
from mote.common.interface.event_subscriber import ObservationSubscriber
from mote.common.schema import AIMessage, ContextManagerConfig, UserMessage
from mote.context.manager import ContextManager


class _FakeLLM:
    def __init__(self, *, summary: str = "sum", model: str = "m"):
        self.model = model
        self._summary = summary

    async def aask(self, msg=None, system_msgs=None, stream=True, **kwargs) -> str:
        return self._summary


from mote.session.events import COMPACTED, MESSAGE
from mote.session.log import SessionLog
from mote.session.subscribers import RecorderSubscriber


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
    await recorder.handle(CompactionCheckpointEvent(messages=[UserMessage(content="[summary]")], summary="s"))
    # iter_raw() drains the DiskWriter first, so queued writes are on disk here.
    types = [r["type"] for r in log.iter_raw()]
    assert types == [MESSAGE, COMPACTED]


@pytest.mark.asyncio
async def test_recorder_persists_typed_output_lifecycle(tmp_path):
    from mote.common.events import (
        OutputAcceptedEvent,
        OutputCandidateReceivedEvent,
        OutputCommitStartedEvent,
        OutputCommittedEvent,
        OutputPublicationQueuedEvent,
        OutputPublishedEvent,
        OutputValidationRejectedEvent,
    )
    from mote.session.events import (
        OUTPUT_ACCEPTED,
        OUTPUT_CANDIDATE_RECEIVED,
        OUTPUT_COMMIT_STARTED,
        OUTPUT_COMMITTED,
        OUTPUT_PUBLICATION_QUEUED,
        OUTPUT_PUBLISHED,
        OUTPUT_VALIDATION_REJECTED,
    )

    log = SessionLog("sess_output", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    bus = _bus_with(recorder)

    await bus.observe(
        OutputCandidateReceivedEvent(candidate_id="c1", contract_id="test.report@1", raw={"count": "bad"})
    )
    await bus.observe(
        OutputValidationRejectedEvent(
            candidate_id="c1",
            contract_id="test.report@1",
            correction_attempt=1,
            corrections_remaining=0,
        )
    )
    await bus.observe(OutputAcceptedEvent(candidate_id="c2", contract_id="test.report@1", value={"count": 1}))
    await bus.observe(OutputCommitStartedEvent(candidate_id="c2", contract_id="test.report@1"))
    await bus.observe(OutputCommittedEvent(candidate_id="c2", contract_id="test.report@1", value={"count": 1}))
    await bus.observe(
        OutputPublicationQueuedEvent(publication_id="pub-1", candidate_id="c2", contract_id="test.report@1")
    )
    await bus.observe(OutputPublishedEvent(candidate_id="c2", contract_id="test.report@1"))

    assert [record["type"] for record in log.iter_raw()] == [
        OUTPUT_CANDIDATE_RECEIVED,
        OUTPUT_VALIDATION_REJECTED,
        OUTPUT_ACCEPTED,
        OUTPUT_COMMIT_STARTED,
        OUTPUT_COMMITTED,
        OUTPUT_PUBLICATION_QUEUED,
        OUTPUT_PUBLISHED,
    ]


@pytest.mark.asyncio
async def test_checkpoint_drain_persists_tool_call_before_crash(tmp_path):
    # The pre-execution durability checkpoint (react loop, EXTERNAL-effect path):
    # the assistant tool-call message is appended, then get_disk_writer().drain()
    # flushes it. Simulate a crash right after that drain (before any tool result
    # is recorded) and confirm a replay can still read the assistant tool_calls —
    # so a resume has the call id it needs to reconcile the dangling call.
    from mote.common.disk import get_disk_writer
    from mote.session.events import parse_event

    log = SessionLog("sess_ckpt", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    cm = ContextManager(bus=_bus_with(recorder))

    # record_call's effect: assistant message carrying the tool_calls, no results.
    await cm.add(
        AIMessage(
            content="calling",
            tool_calls=[{"id": "t1", "name": "Bash", "args": {"cmd": "curl x"}}],
        )
    )
    # The loop's checkpoint barrier — the durable flush before the side effect.
    await get_disk_writer().drain()

    # "Crash": nothing else is written. Replay reads the rollout back.
    events = [parse_event(r) for r in log.iter_raw()]
    assert [e.type for e in events] == [MESSAGE]
    calls = events[0].message.metadata.get("tool_calls")
    assert calls == [{"id": "t1", "name": "Bash", "args": {"cmd": "curl x"}}]


@pytest.mark.asyncio
async def test_disabled_recorder_does_not_append(tmp_path):
    log = SessionLog("sess_off", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log, enabled=False)
    cm = ContextManager(bus=_bus_with(recorder))
    await cm.add(UserMessage(content="ignored"))
    await recorder.handle(CompactionCheckpointEvent(messages=[UserMessage(content="x")], summary="s"))
    assert list(log.iter_raw()) == []


@pytest.mark.asyncio
async def test_llm_response_persists_compact_call_record(tmp_path):
    from mote.common.events import LLMResponseEvent
    from mote.session.events import LLM_CALL, parse_event

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
    from mote.common.events import LLMResponseEvent

    log = SessionLog("sess_llm_empty", base_dir=str(tmp_path))
    recorder = RecorderSubscriber(log)
    await recorder.handle(LLMResponseEvent(request_id="rq2", model="gpt-4o", usage=None))
    assert list(log.iter_raw()) == []


class TestHistoryEditedPersistence:
    """A react-unit delete emits a ``HistoryEditedEvent``; the recorder persists it
    as a ``CompactedEvent`` so replay resets history to the pruned list (durable
    across restart/resume) WITHOUT a compaction UI marker (the projector ignores
    the source event — verified separately in the cli view-isolation tests)."""

    @pytest.mark.asyncio
    async def test_history_edited_recorded_as_compacted(self, tmp_path):
        from mote.common.events import HistoryEditedEvent
        from mote.session.events import COMPACTED

        log = SessionLog("sess_edit", base_dir=str(tmp_path))
        recorder = RecorderSubscriber(log)
        await recorder.handle(HistoryEditedEvent(messages=[UserMessage(content="kept")], reason="delete"))
        types = [r["type"] for r in log.iter_raw()]
        assert types == [COMPACTED]

    @pytest.mark.asyncio
    async def test_replay_resets_history_to_pruned_messages(self, tmp_path):
        from mote.common.events import HistoryEditedEvent
        from mote.session.replay import replay

        log = SessionLog("sess_edit_replay", base_dir=str(tmp_path))
        recorder = RecorderSubscriber(log)
        # Original three-turn history lands as message records.
        cm = ContextManager(bus=_bus_with(recorder))
        await cm.add(UserMessage(content="q1"))
        await cm.add(AIMessage(content="a1"))
        await cm.add(UserMessage(content="q2"))
        await cm.add(AIMessage(content="a2"))
        # Delete the first react-unit -> pruned list is [q2, a2].
        await recorder.handle(
            HistoryEditedEvent(
                messages=[UserMessage(content="q2"), AIMessage(content="a2")],
                reason="delete",
            )
        )
        result = replay(log)
        # The checkpoint resets replay history to the pruned list; the pre-edit
        # q1/a1 appends are discarded (same reset semantics as a compaction).
        assert [m.content for m in result.messages] == ["q2", "a2"]
        assert result.from_checkpoint is True
        assert result.checkpoints == 1

    @pytest.mark.asyncio
    async def test_round_trip_append_edit_replay(self, tmp_path):
        """append -> edit -> replay yields the post-delete history, and later
        appends after the edit stack onto the pruned list."""
        from mote.common.events import HistoryEditedEvent
        from mote.session.replay import replay

        log = SessionLog("sess_edit_rt", base_dir=str(tmp_path))
        recorder = RecorderSubscriber(log)
        cm = ContextManager(bus=_bus_with(recorder))
        await cm.add(UserMessage(content="q1"))
        await cm.add(AIMessage(content="a1"))
        await cm.add(UserMessage(content="q2"))
        await cm.add(AIMessage(content="a2"))
        await recorder.handle(
            HistoryEditedEvent(
                messages=[UserMessage(content="q1"), AIMessage(content="a1")],
                reason="delete",
            )
        )
        # A new turn continues after the delete.
        await cm.add(UserMessage(content="q3"))
        await cm.add(AIMessage(content="a3"))
        result = replay(log)
        assert [m.content for m in result.messages] == ["q1", "a1", "q3", "a3"]


class TestToolMessageToolReferences:
    """``ToolMessage.tool_references`` (server-side tool-search discovery) survives
    dump/load and surfaces as the ``_tool_references`` private wire key — only
    when set (metadata-as-truth, invisible to every existing path otherwise)."""

    def test_round_trips_through_dump_load(self):
        from mote.common.const import TOOL_REFERENCES
        from mote.common.schema import Message, ToolMessage

        msg = ToolMessage(
            content="revealed",
            tool_call_id="c1",
            tool_references=["ConvertImage", "QueryDatabase"],
        )
        restored = Message.load(msg.dump())
        assert restored is not None
        # Subclass identity is lost on replay; metadata is the truth.
        assert restored.metadata[TOOL_REFERENCES] == ["ConvertImage", "QueryDatabase"]

    def test_to_dict_emits_private_key_only_when_set(self):
        from mote.common.schema import ToolMessage

        with_refs = ToolMessage(content="x", tool_call_id="c1", tool_references=["A"])
        assert with_refs.to_dict()["_tool_references"] == ["A"]

        without = ToolMessage(content="x", tool_call_id="c2")
        assert "_tool_references" not in without.to_dict()
