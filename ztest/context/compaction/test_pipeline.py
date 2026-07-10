#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the :class:`ReductionPipeline` — cheapest-first, stop when met.

The pipeline is pure orchestration. These tests use tiny fake reducers (a fixed
``cost`` + a scripted ``reduce``) to assert the three run rules independent of
the real fold/summarize/drop logic:

- FREE reducers always run (opportunistically), even when already under target.
- Costly reducers run only while still over target, and the pipeline stops as
  soon as one meets it (no wasted LLM / drop).
- DESTRUCTIVE runs only under HARD urgency; SOFT stops before it.
"""
from __future__ import annotations

import asyncio

from metagpt.context.compaction.pipeline import ReductionPipeline
from metagpt.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from metagpt.context.compaction.request import ReductionRequest, Urgency
from metagpt.context.compaction.transcript import Transcript

from ..conftest import text_msg


def _run(coro):
    return asyncio.run(coro)


class RecordingReducer:
    """A fake reducer that records if it ran and optionally rewrites the transcript."""

    def __init__(self, cost, *, new_transcript=None, summary=None, strategy="fake"):
        self.cost = cost
        self.ran = False
        self._new = new_transcript
        self._summary = summary
        self._strategy = strategy

    async def reduce(self, transcript, request):
        self.ran = True
        out = self._new if self._new is not None else transcript
        changed = self._new is not None
        return ReductionOutcome(out, changed=changed, strategy=self._strategy, summary=self._summary)


def _big():
    # Content-heavy transcript so token_count comfortably exceeds a small target.
    return Transcript.from_messages([text_msg(("word " * 50) + f" {i}") for i in range(6)])


def _tiny():
    return Transcript.from_messages([text_msg("hi")])


def test_reducers_sorted_by_cost():
    a = RecordingReducer(ReducerCost.DESTRUCTIVE)
    b = RecordingReducer(ReducerCost.FREE)
    c = RecordingReducer(ReducerCost.LLM)
    p = ReductionPipeline([a, b, c], model="gpt-4")
    assert [r.cost for r in p._reducers] == [ReducerCost.FREE, ReducerCost.LLM, ReducerCost.DESTRUCTIVE]


def test_free_runs_even_when_under_target():
    free = RecordingReducer(ReducerCost.FREE)
    p = ReductionPipeline([free], model="gpt-4")
    _run(p.run(_tiny(), ReductionRequest(target_tokens=10_000_000)))
    assert free.ran is True


def test_costly_skipped_when_already_under_target():
    llm = RecordingReducer(ReducerCost.LLM)
    p = ReductionPipeline([llm], model="gpt-4")
    _run(p.run(_tiny(), ReductionRequest(target_tokens=10_000_000)))
    assert llm.ran is False


def test_costly_runs_when_over_target():
    small = Transcript.from_messages([text_msg("short")])
    llm = RecordingReducer(ReducerCost.LLM, new_transcript=small, summary="S")
    p = ReductionPipeline([llm], model="gpt-4")
    out = _run(p.run(_big(), ReductionRequest(target_tokens=1)))
    assert llm.ran is True
    assert out.changed is True
    assert out.summary == "S"


def test_stops_after_target_met_before_destructive():
    small = Transcript.from_messages([text_msg("x")])
    llm = RecordingReducer(ReducerCost.LLM, new_transcript=small)  # brings under target
    drop = RecordingReducer(ReducerCost.DESTRUCTIVE)
    p = ReductionPipeline([llm, drop], model="gpt-4")
    out = _run(p.run(_big(), ReductionRequest(target_tokens=1, urgency=Urgency.HARD)))
    assert llm.ran is True
    assert drop.ran is False  # target met by summarize -> drop never runs
    assert out.target_met is True


def test_soft_never_reaches_destructive():
    # summarize does nothing (no rewrite), still over target; SOFT must stop
    # before the destructive reducer.
    llm = RecordingReducer(ReducerCost.LLM)
    drop = RecordingReducer(ReducerCost.DESTRUCTIVE)
    p = ReductionPipeline([llm, drop], model="gpt-4")
    _run(p.run(_big(), ReductionRequest(target_tokens=1, urgency=Urgency.SOFT)))
    assert llm.ran is True
    assert drop.ran is False


def test_hard_reaches_destructive_when_still_over():
    small = Transcript.from_messages([text_msg("x")])
    llm = RecordingReducer(ReducerCost.LLM)  # no-op
    drop = RecordingReducer(ReducerCost.DESTRUCTIVE, new_transcript=small, strategy="head_drop")
    p = ReductionPipeline([llm, drop], model="gpt-4")
    out = _run(p.run(_big(), ReductionRequest(target_tokens=1, urgency=Urgency.HARD)))
    assert llm.ran is True
    assert drop.ran is True
    assert out.strategy == "head_drop"
