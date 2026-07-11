#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the FREE :class:`EraseReducer` — true pair-deletion of erasable results.

Erase is the counterpart to fold: instead of shrinking a reconstructable result's
body to a placeholder (pairing intact, re-derivable), it removes the result
*and* its ``tool_call`` entry together, so the tool_use↔tool_result pairing stays
legal. It touches only results the producer tagged ``RETENTION_ERASABLE`` and is
pressure-gated (fires only when the transcript is over target). Results tagged
``RETENTION_PIN`` are never touched here and are additionally protected against
fold / summarize / drop by ``Segment.pinned``.
"""
from __future__ import annotations

import asyncio

from mote.common.const import RETENTION, RETENTION_ERASABLE, RETENTION_PIN, TOOL_CALL_ID, TOOL_CALLS
from mote.common.schema import AIMessage, ContextManagerConfig
from mote.context.compaction.reducers.erase import EraseReducer
from mote.context.compaction.reducers.fold import FoldReducer
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import SegmentKind, Transcript

from ..conftest import COMPACTABLE, make_pairs, tool_call_msg, tool_result_msg


def _run(coro):
    return asyncio.run(coro)


def _transcript(msgs):
    return Transcript.from_messages(msgs, compactable=COMPACTABLE)


def _erase(transcript, *, target=0):
    # target=0 forces the pressure gate open (any real history is over 0 tokens).
    reducer = EraseReducer(ContextManagerConfig(), model="gpt-4")
    return _run(reducer.reduce(transcript, ReductionRequest(target_tokens=target)))


def _mark_erasable(msg):
    msg.add_metadata(RETENTION, RETENTION_ERASABLE)
    return msg


def _result_ids(transcript):
    return [m.metadata.get(TOOL_CALL_ID) for m in transcript.to_messages() if m.metadata.get(TOOL_CALL_ID)]


def _multi_call_turn(call_ids, *, content="", name="Read"):
    """An assistant turn invoking several tools + their result messages."""
    calls = [{"id": cid, "name": name, "args": {}} for cid in call_ids]
    a = AIMessage(content=content)
    a.add_metadata(TOOL_CALLS, calls)
    results = [tool_result_msg(cid, f"body-{cid}") for cid in call_ids]
    return a, results


# ---------------------------------------------------------------------------
# Pressure gate
# ---------------------------------------------------------------------------


def test_under_target_is_noop():
    msgs = make_pairs(3)
    for m in msgs:
        if m.metadata.get(TOOL_CALL_ID):
            _mark_erasable(m)
    t = _transcript(msgs)
    # A huge target keeps us under pressure → nothing erased even though tagged.
    out = _erase(t, target=10_000_000)
    assert out.changed is False
    assert len(_result_ids(out.transcript)) == 3


def test_over_target_erases_tagged_pairs():
    msgs = make_pairs(3)
    _mark_erasable(msgs[1])  # result of id-0
    t = _transcript(msgs)
    out = _erase(t)
    assert out.changed is True
    # The erasable pair is gone entirely — NOT replaced with a placeholder.
    assert "id-0" not in _result_ids(out.transcript)
    assert out.tokens_freed > 0
    assert out.strategy == "erase"


def test_no_tagged_results_is_noop_even_over_target():
    t = _transcript(make_pairs(3))
    out = _erase(t)
    assert out.changed is False
    assert len(_result_ids(out.transcript)) == 3


def test_pinned_results_are_never_erased():
    msgs = make_pairs(2)
    msgs[1].add_metadata(RETENTION, RETENTION_PIN)  # result of id-0 pinned
    t = _transcript(msgs)
    out = _erase(t)
    # PIN != ERASABLE, so it is not collected; nothing to erase.
    assert out.changed is False
    assert "id-0" in _result_ids(out.transcript)


# ---------------------------------------------------------------------------
# Pairing invariant on partial / whole-turn erasure
# ---------------------------------------------------------------------------


def test_partial_erase_keeps_pairing_within_a_turn():
    a, results = _multi_call_turn(["c1", "c2", "c3"])
    _mark_erasable(results[1])  # erase only c2
    t = _transcript([a, *results])
    out = _erase(t)
    assert out.changed is True

    msgs = out.transcript.to_messages()
    # Assistant keeps c1 + c3 only.
    kept_calls = [c["id"] for c in msgs[0].metadata[TOOL_CALLS]]
    assert kept_calls == ["c1", "c3"]
    # And exactly those two results remain — every call still has its result.
    assert _result_ids(out.transcript) == ["c1", "c3"]


def test_whole_turn_collapses_when_all_calls_erased_and_blank():
    a, results = _multi_call_turn(["c1", "c2"], content="")
    for r in results:
        _mark_erasable(r)
    t = _transcript([a, *results])
    out = _erase(t)
    assert out.changed is True
    # Nothing meaningful left → the entire tool group is dropped.
    assert out.transcript.to_messages() == []


def test_assistant_prose_survives_when_all_calls_erased():
    a, results = _multi_call_turn(["c1", "c2"], content="here is my reasoning")
    for r in results:
        _mark_erasable(r)
    t = _transcript([a, *results])
    out = _erase(t)
    assert out.changed is True
    msgs = out.transcript.to_messages()
    # The assistant message survives (prose preserved) with no calls and no
    # dangling results.
    assert len(msgs) == 1
    assert msgs[0].content == "here is my reasoning"
    assert msgs[0].metadata[TOOL_CALLS] == []
    assert _result_ids(out.transcript) == []


# ---------------------------------------------------------------------------
# Transcript.erase_pairs primitive
# ---------------------------------------------------------------------------


def test_erase_pairs_empty_ids_returns_self():
    t = _transcript(make_pairs(2))
    assert t.erase_pairs([]) is t


def test_erase_pairs_preserves_reconstructable_flag():
    a, results = _multi_call_turn(["c1", "c2"], name="Read")
    t = _transcript([a, *results])
    assert t.segments[0].reconstructable is True
    out = t.erase_pairs(["c1"])
    assert out.segments[0].kind is SegmentKind.TOOL_GROUP
    assert out.segments[0].reconstructable is True
