#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the :class:`ContextEngine` — the compaction event lifecycle.

The engine wraps a pipeline with: PreCompact (veto / instruction supply) up
front, and — only on a successful summarize — CompactionCheckpoint + PostCompact.
A fake bus records emitted events and scripts the PreCompact outcome; the
pipeline is a real one driven by a scripted reducer.
"""
from __future__ import annotations

import asyncio

from mote.common.events import CompactionCheckpointEvent, PostCompactEvent, PreCompactEvent
from mote.common.events.outcomes import CompactOutcome
from mote.context.compaction.engine import ContextEngine
from mote.context.compaction.pipeline import ReductionPipeline
from mote.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.context.compaction.request import ReductionRequest, Urgency
from mote.context.compaction.transcript import Transcript

from ..conftest import text_msg


def _run(coro):
    return asyncio.run(coro)


class FakeBus:
    def __init__(self, pre_outcome=None):
        self.events = []
        self._pre = pre_outcome

    async def emit(self, event):
        self.events.append(event)
        if isinstance(event, PreCompactEvent):
            return self._pre
        return None


class ScriptedReducer:
    def __init__(self, cost, *, new_transcript=None, summary=None):
        self.cost = cost
        self.ran = False
        self._new = new_transcript
        self._summary = summary

    async def reduce(self, transcript, request):
        self.ran = True
        out = self._new if self._new is not None else transcript
        return ReductionOutcome(out, changed=self._new is not None, strategy="summarize", summary=self._summary)


class SummarizeStub:
    """Stands in for the summarize reducer so the engine can set instructions."""

    cost = ReducerCost.LLM

    def __init__(self):
        self.custom_instructions = None
        self.ran = False

    async def reduce(self, transcript, request):
        self.ran = True
        rebuilt = Transcript.from_messages([text_msg("[summary]")])
        return ReductionOutcome(rebuilt, changed=True, strategy="summarize", summary="SUMMARY")


def _big():
    return Transcript.from_messages([text_msg(("word " * 50) + f" {i}") for i in range(6)])


def _engine(bus=None, summarize=None, reducers=None):
    reducers = reducers if reducers is not None else [ScriptedReducer(ReducerCost.LLM)]
    pipeline = ReductionPipeline(reducers, model="gpt-4")
    return ContextEngine(pipeline, bus=bus, summarize_reducer=summarize)


def test_summary_emits_checkpoint_and_postcompact():
    bus = FakeBus()
    summarize = SummarizeStub()
    engine = _engine(bus=bus, summarize=summarize, reducers=[summarize])
    out = _run(engine.reduce(_big(), ReductionRequest(target_tokens=1)))
    assert out.summary == "SUMMARY"
    kinds = [type(e) for e in bus.events]
    assert PreCompactEvent in kinds
    assert CompactionCheckpointEvent in kinds
    assert PostCompactEvent in kinds
    # checkpoint carries the rebuilt history.
    ckpt = next(e for e in bus.events if isinstance(e, CompactionCheckpointEvent))
    assert ckpt.summary == "SUMMARY"
    assert [m.content for m in ckpt.messages] == ["[summary]"]


def test_no_summary_emits_no_checkpoint():
    bus = FakeBus()
    # scripted reducer that changes nothing and produces no summary.
    engine = _engine(bus=bus, reducers=[ScriptedReducer(ReducerCost.LLM)])
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1)))
    kinds = [type(e) for e in bus.events]
    assert PreCompactEvent in kinds
    assert CompactionCheckpointEvent not in kinds
    assert PostCompactEvent not in kinds


def test_precompact_veto_skips_pipeline():
    bus = FakeBus(pre_outcome=CompactOutcome(cancel=True))
    reducer = ScriptedReducer(ReducerCost.LLM)
    engine = _engine(bus=bus, reducers=[reducer])
    out = _run(engine.reduce(_big(), ReductionRequest(target_tokens=1)))
    assert reducer.ran is False
    assert out.changed is False


def test_precompact_supplies_custom_instructions():
    bus = FakeBus(pre_outcome=CompactOutcome(additional_context=["FOCUS ON X"]))
    summarize = SummarizeStub()
    engine = _engine(bus=bus, summarize=summarize, reducers=[summarize])
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1), custom_instructions="original"))
    assert summarize.custom_instructions == "FOCUS ON X"


def test_caller_instructions_used_without_hook():
    summarize = SummarizeStub()
    engine = _engine(bus=None, summarize=summarize, reducers=[summarize])
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1), custom_instructions="caller says"))
    assert summarize.custom_instructions == "caller says"
