#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ContextEngine policy enforcement and compaction facts."""

from __future__ import annotations

import asyncio

from mote.contracts.conversation.compaction_policy import CompactionDecision
from mote.contracts.events.conversation import ContextCompactedEvent, PostCompactEvent
from mote.runtime.context.compaction.engine import ContextEngine
from mote.runtime.context.compaction.pipeline import ReductionPipeline
from mote.runtime.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.runtime.context.compaction.request import ReductionRequest, Urgency
from mote.runtime.context.compaction.transcript import Transcript

from ..conftest import text_msg


def _run(coro):
    return asyncio.run(coro)


class FakeTelemetry:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return None


class RecordingFactSink:
    def __init__(self):
        self.events = []

    async def commit_fact(self, event):
        self.events.append(event)


class FixedPolicy:
    def __init__(self, decision):
        self.decision = decision
        self.intent = None

    async def process(self, intent):
        self.intent = intent
        return self.decision


class ScriptedReducer:
    def __init__(
        self,
        cost,
        *,
        new_transcript=None,
        summary=None,
        strategy="summarize",
    ):
        self.cost = cost
        self.ran = False
        self._new = new_transcript
        self._summary = summary
        self._strategy = strategy

    async def reduce(self, transcript, request):
        self.ran = True
        out = self._new if self._new is not None else transcript
        return ReductionOutcome(
            out,
            changed=self._new is not None,
            strategy=self._strategy,
            summary=self._summary,
        )


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


def _engine(
    telemetry=None,
    summarize=None,
    reducers=None,
    policy=None,
    session_fact_sink=None,
):
    reducers = reducers if reducers is not None else [ScriptedReducer(ReducerCost.LLM)]
    pipeline = ReductionPipeline(reducers, model="gpt-4")
    return ContextEngine(
        pipeline,
        telemetry=telemetry,
        summarize_reducer=summarize,
        policy=policy,
        session_fact_sink=session_fact_sink,
    )


def test_summary_commits_projection_and_emits_postcompact():
    telemetry = FakeTelemetry()
    summarize = SummarizeStub()
    engine = _engine(telemetry=telemetry, summarize=summarize, reducers=[summarize])
    out = _run(engine.reduce(_big(), ReductionRequest(target_tokens=1)))
    assert out.summary == "SUMMARY"
    kinds = [type(e) for e in telemetry.events]
    assert kinds == [ContextCompactedEvent, PostCompactEvent]
    compacted = next(event for event in telemetry.events if isinstance(event, ContextCompactedEvent))
    assert compacted.summary == "SUMMARY"
    assert [m.content for m in compacted.model_context_messages] == ["[summary]"]
    assert compacted.source_message_ids
    assert compacted.strategy == "summarize"


def test_unchanged_reduction_emits_no_compaction():
    telemetry = FakeTelemetry()
    # scripted reducer that changes nothing and produces no summary.
    engine = _engine(telemetry=telemetry, reducers=[ScriptedReducer(ReducerCost.LLM)])
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1)))
    kinds = [type(e) for e in telemetry.events]
    assert kinds == []


def test_changed_reduction_without_summary_commits_projection():
    telemetry = FakeTelemetry()
    sink = RecordingFactSink()
    rebuilt = Transcript.from_messages([text_msg("folded")])
    reducer = ScriptedReducer(
        ReducerCost.FREE,
        new_transcript=rebuilt,
        strategy="fold",
    )
    engine = _engine(
        telemetry=telemetry,
        reducers=[reducer],
        session_fact_sink=sink,
    )

    out = _run(engine.reduce(_big(), ReductionRequest(target_tokens=10_000)))

    assert out.changed is True
    assert [type(event) for event in sink.events] == [ContextCompactedEvent]
    compacted = sink.events[0]
    assert [message.content for message in compacted.model_context_messages] == ["folded"]
    assert compacted.summary == ""
    assert compacted.strategy == "fold"
    assert [type(event) for event in telemetry.events] == [
        ContextCompactedEvent,
        PostCompactEvent,
    ]


def test_policy_supplies_custom_instructions():
    policy = FixedPolicy(
        CompactionDecision(
            profile="balanced",
            custom_instructions="FOCUS ON X",
        )
    )
    summarize = SummarizeStub()
    engine = _engine(summarize=summarize, reducers=[summarize], policy=policy)
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1), custom_instructions="original"))
    assert summarize.custom_instructions == "FOCUS ON X"
    assert policy.intent.custom_instructions == "original"


def test_preserve_policy_blocks_destructive_reducer():
    policy = FixedPolicy(CompactionDecision(profile="preserve", allow_destructive=False))
    reducer = ScriptedReducer(
        ReducerCost.DESTRUCTIVE,
        new_transcript=Transcript.from_messages([text_msg("kept")]),
    )
    engine = _engine(reducers=[reducer], policy=policy)
    out = _run(
        engine.reduce(
            _big(),
            ReductionRequest(target_tokens=1, urgency=Urgency.HARD),
        )
    )
    assert reducer.ran is False
    assert out.changed is False


def test_caller_instructions_used_without_hook():
    summarize = SummarizeStub()
    engine = _engine(telemetry=None, summarize=summarize, reducers=[summarize])
    _run(engine.reduce(_big(), ReductionRequest(target_tokens=1), custom_instructions="caller says"))
    assert summarize.custom_instructions == "caller says"
