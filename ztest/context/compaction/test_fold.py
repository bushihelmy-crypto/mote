#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the FREE :class:`FoldReducer` — count-gated in-place body fold.

This is the old ``microcompact`` behavior expressed as a reducer: once more than
``trigger`` reconstructable tool results have piled up, clear all but the most
recent ``keep_recent``. It shrinks ``Message.content`` in place (leaving the
tool_call↔tool_result pairing fully intact) and never touches sticky resource
bodies or non-reconstructable (conversational) results.
"""
from __future__ import annotations

import asyncio

from metagpt.common.const import RESOURCE_STICKY, RETENTION, RETENTION_PIN
from metagpt.common.const.context import TOOL_RESULT_CLEARED_MESSAGE
from metagpt.common.schema import ContextManagerConfig
from metagpt.context.compaction.reducers.fold import FoldReducer
from metagpt.context.compaction.request import ReductionRequest
from metagpt.context.compaction.transcript import Transcript

from ..conftest import COMPACTABLE, make_pairs, tool_call_msg, tool_result_msg

PLACEHOLDER = TOOL_RESULT_CLEARED_MESSAGE


def _run(coro):
    return asyncio.run(coro)


def _cfg(**kw) -> ContextManagerConfig:
    # clear_at_least=0 isolates the count-gate tests from the token-gate (which
    # has its own test below); make_pairs bodies are tiny, so the real default
    # would otherwise no-op every fold here.
    base = dict(microcompact_trigger_threshold=3, microcompact_keep_recent=1, microcompact_clear_at_least=0)
    base.update(kw)
    return ContextManagerConfig(**base)


def _transcript(msgs):
    # The reconstructable judgment is made once, here, by from_messages — the
    # FoldReducer only consumes the resulting segment flag.
    return Transcript.from_messages(msgs, compactable=COMPACTABLE)


def _fold(transcript, cfg=None, *, target=10_000_000):
    reducer = FoldReducer(cfg or _cfg(), model="gpt-4")
    req = ReductionRequest(target_tokens=target)
    return _run(reducer.reduce(transcript, req))


def _contents(transcript):
    return [m.content for m in transcript.to_messages() if m.metadata.get("tool_call_id")]


def test_disabled_is_noop():
    t = _transcript(make_pairs(5))
    out = _fold(t, _cfg(enable_microcompact=False))
    assert out.changed is False
    assert all(c != PLACEHOLDER for c in _contents(out.transcript))


def test_below_trigger_is_noop():
    # 3 results, trigger=3 -> len(active) <= trigger, nothing folded.
    t = _transcript(make_pairs(3))
    out = _fold(t)
    assert out.changed is False
    assert all(c != PLACEHOLDER for c in _contents(out.transcript))


def test_over_trigger_folds_all_but_keep_recent():
    # 5 results > trigger 3 -> clear 5 - keep_recent(1) = 4 oldest, keep the last.
    t = _transcript(make_pairs(5))
    out = _fold(t)
    assert out.changed is True
    contents = _contents(out.transcript)
    assert contents[:4] == [PLACEHOLDER] * 4
    assert contents[4] != PLACEHOLDER
    assert out.tokens_freed > 0


def test_sticky_results_are_never_folded():
    msgs = make_pairs(5)
    # Mark one result sticky — it must survive folding untouched, and it is
    # excluded from the active count entirely.
    sticky_result = tool_result_msg("sticky-id", "PRECIOUS BODY")
    sticky_result.add_metadata(RESOURCE_STICKY, True)
    sticky_call = tool_call_msg("sticky-id", "Read")
    msgs = [sticky_call, sticky_result, *msgs]
    t = _transcript(msgs)

    out = _fold(t)
    kept = [m.content for m in out.transcript.to_messages() if m.metadata.get(RESOURCE_STICKY)]
    assert kept == ["PRECIOUS BODY"]


def test_pinned_results_are_never_folded():
    # A RETENTION_PIN result must survive folding untouched even though its tool
    # is reconstructable — the model asked to keep this body verbatim.
    msgs = make_pairs(5)
    msgs[1].add_metadata(RETENTION, RETENTION_PIN)  # result of id-0
    t = _transcript(msgs)
    out = _fold(t)
    survivor = [m.content for m in out.transcript.to_messages() if m.metadata.get(RETENTION) == RETENTION_PIN]
    assert survivor == ["x" * 200]
    assert PLACEHOLDER not in survivor


def test_non_reconstructable_tools_not_folded():
    # AskUserQuestion is not compactable -> its result never enters the fold set.
    ask_call = tool_call_msg("q", "AskUserQuestion")
    ask_res = tool_result_msg("q", "the human answer")
    t = _transcript([ask_call, ask_res, *make_pairs(5)])
    out = _fold(t)
    survivors = [m.content for m in out.transcript.to_messages() if m.metadata.get("tool_call_id") == "q"]
    assert survivors == ["the human answer"]


def test_target_met_reported():
    t = _transcript(make_pairs(5))
    out = _fold(t, target=10_000_000)
    assert out.target_met is True
    assert out.strategy == "fold"


def test_clear_at_least_gates_trivial_folds():
    # 5 tiny results are over the count trigger, but folding them frees far fewer
    # than clear_at_least tokens — so the fold is skipped to keep the cache warm
    # (folding would force a one-time prefix-cache write not worth the trim).
    t = _transcript(make_pairs(5))  # each body ~200 chars → tens of tokens
    out = _fold(t, _cfg(microcompact_clear_at_least=10_000))
    assert out.changed is False
    assert out.tokens_freed == 0
    assert all(c != PLACEHOLDER for c in _contents(out.transcript))


def test_clear_at_least_folds_when_worth_it():
    # Big bodies clear well above the threshold → the fold proceeds as usual.
    t = _transcript(make_pairs(5, result="y" * 8_000))
    out = _fold(t, _cfg(microcompact_clear_at_least=1_000))
    assert out.changed is True
    assert out.tokens_freed >= 1_000
    contents = _contents(out.transcript)
    assert contents[:4] == [PLACEHOLDER] * 4
    assert contents[4] != PLACEHOLDER
