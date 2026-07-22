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

from mote.common.const import RESOURCE_STICKY, RETENTION, RETENTION_PIN, TOOL_CALLS
from mote.common.const.context import FOLDED_WRITE_MARKER, TOOL_RESULT_CLEARED_MESSAGE
from mote.common.schema import ContextManagerConfig
from mote.context.compaction.reducers.fold import FoldReducer
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import Transcript

from ..conftest import COMPACTABLE, make_pairs, tool_call_msg, tool_result_msg

PLACEHOLDER = TOOL_RESULT_CLEARED_MESSAGE

# Names routing to Edit (the record-time raw name may be any alias); the reducer
# folds a write only when its call name is in this set.
WRITE_FOLD_NAMES = frozenset({"Edit", "write", "Update"})

_BIG_BODY = "x = 1\n" * 900


def write_pair(call_id: str, *, name: str = "Edit", body: str = _BIG_BODY, old: str = "", file: str = "x.py"):
    """An Edit whole-file-write assistant call + its (reconstructable) result."""
    call = tool_call_msg(call_id, name, args={"file_path": file, "old_string": old, "new_string": body})
    return [call, tool_result_msg(call_id, "wrote x.py")]


def make_writes(n: int, *, name: str = "Edit", body: str = _BIG_BODY, start: int = 0):
    """``n`` consecutive Edit whole-file-write (call, result) pairs."""
    msgs = []
    for i in range(start, start + n):
        msgs += write_pair(f"w-{i}", name=name, body=body)
    return msgs


def _new_strings(transcript):
    """Every recorded call's ``new_string`` (in order), for asserting write folds."""
    out = []
    for m in transcript.to_messages():
        for c in m.metadata.get(TOOL_CALLS) or []:
            args = c.get("args") or {}
            if "new_string" in args:
                out.append(args["new_string"])
    return out


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


def _fold(transcript, cfg=None, *, target=10_000_000, write_fold_names=frozenset()):
    reducer = FoldReducer(cfg or _cfg(), model="gpt-4", write_fold_names=write_fold_names)
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


# ---------------------------------------------------------------------------
# Edit whole-file-write ``new_string`` fold (moved here from the record-time
# args limiter): folded at the paired result's boundary, under the SAME count/token gate as
# tool-result bodies, to the neutral FOLDED_WRITE_MARKER.
# ---------------------------------------------------------------------------


def test_write_fold_requires_names_injected():
    # With NO write_fold_names the reducer never touches a write arg, even for a
    # large whole-file write over the count trigger — the mechanism is gated on
    # the Role having threaded Edit's alias set (empty in standalone/test use).
    t = _transcript(make_writes(5))
    out = _fold(t)  # write_fold_names defaults to empty
    assert all(s == _BIG_BODY for s in _new_strings(out.transcript))


def test_write_folds_over_trigger_keeping_recent():
    # 5 writes > trigger 3 → fold the 5 - keep_recent(1) = 4 oldest new_strings to
    # the marker, keep the most recent verbatim. Rides the SAME results count gate
    # (each write emits a reconstructable result).
    t = _transcript(make_writes(5))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    assert out.changed is True
    strings = _new_strings(out.transcript)
    assert strings[:4] == [FOLDED_WRITE_MARKER] * 4
    assert strings[4] == _BIG_BODY
    assert out.tokens_freed > 0


def test_sparse_old_write_folds_with_its_result_boundary():
    # Edit is sparse: an old write followed by four Read results is still older
    # than the one-result working set. Its new_string must fold with its paired
    # result; keeping the latest N *writes* would incorrectly preserve it.
    msgs = [*write_pair("w-old"), *make_pairs(4)]
    t = _transcript(msgs)
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)

    assert out.changed is True
    assert _new_strings(out.transcript) == [FOLDED_WRITE_MARKER]
    assert _contents(out.transcript)[0] == PLACEHOLDER


def test_write_marker_is_neutral():
    t = _transcript(make_writes(5))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    folded = _new_strings(out.transcript)[0]
    assert folded == FOLDED_WRITE_MARKER
    assert "x = 1" not in folded  # no verbatim body
    assert "SUCCEEDED" not in folded  # asserts nothing about success


def test_below_trigger_leaves_writes_verbatim():
    # 3 writes, trigger 3 → nothing folds yet (the deferral: a fresh write stays
    # verbatim while still in the working set).
    t = _transcript(make_writes(3))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    assert out.changed is False
    assert all(s == _BIG_BODY for s in _new_strings(out.transcript))


def test_substring_edit_not_folded():
    # old_string != "" → a substring edit, not a whole-file write; its new_string
    # may be a mid-file fragment and is left verbatim.
    msgs = []
    for i in range(5):
        call = tool_call_msg(f"e-{i}", "Edit", args={"file_path": "x.py", "old_string": "foo", "new_string": _BIG_BODY})
        msgs += [call, tool_result_msg(f"e-{i}", "edited")]
    t = _transcript(msgs)
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    assert all(s == _BIG_BODY for s in _new_strings(out.transcript))


def test_small_write_folds_with_its_result():
    # Size has no separate gate: once the paired result leaves the working set,
    # even a small whole-file write follows it into the fold.
    t = _transcript(make_writes(5, body="y = 2\n"))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    strings = _new_strings(out.transcript)
    assert strings[:4] == [FOLDED_WRITE_MARKER] * 4
    assert strings[4] == "y = 2\n"


def test_non_edit_write_name_not_folded():
    # A large whole-file-write-shaped call whose name is NOT an Edit alias is left
    # alone (only Edit routes whole-file writes).
    t = _transcript(make_writes(5, name="SomeOtherTool"))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    assert all(s == _BIG_BODY for s in _new_strings(out.transcript))


def test_write_fold_idempotent():
    # A second pass over already-folded writes is a no-op (marker != _BIG_BODY and
    # is under the size gate, so it is not re-selected).
    t = _transcript(make_writes(5))
    first = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    second = _fold(first.transcript, write_fold_names=WRITE_FOLD_NAMES)
    assert second.changed is False
    strings = _new_strings(second.transcript)
    assert strings[:4] == [FOLDED_WRITE_MARKER] * 4
    assert strings[4] == _BIG_BODY


def test_write_alias_folds():
    # The record-time raw name may be any Edit alias (write / Update) — all fold.
    t = _transcript(make_writes(5, name="Update"))
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    strings = _new_strings(out.transcript)
    assert strings[:4] == [FOLDED_WRITE_MARKER] * 4


def test_omitted_old_string_folds():
    # A whole-file create may omit old_string entirely; Edit treats a missing key
    # as "", so the fold gate must too.
    msgs = []
    for i in range(5):
        call = tool_call_msg(f"w-{i}", "write", args={"file_path": "x.py", "new_string": _BIG_BODY})
        msgs += [call, tool_result_msg(f"w-{i}", "wrote")]
    t = _transcript(msgs)
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    strings = _new_strings(out.transcript)
    assert strings[:4] == [FOLDED_WRITE_MARKER] * 4


def test_results_and_writes_pool_into_one_token_gate():
    # Writes and result bodies share ONE clear_at_least gate. Tiny bodies + tiny
    # writes below the gate → skip entirely (neither folds). Uses small writes'
    # results but big write bodies to exercise the pooled sum.
    t = _transcript(make_writes(5))
    # A gate far above what 4 folded writes free → nothing folds (pooled sum loses).
    out = _fold(t, _cfg(microcompact_clear_at_least=10_000_000), write_fold_names=WRITE_FOLD_NAMES)
    assert out.changed is False
    assert all(s == _BIG_BODY for s in _new_strings(out.transcript))


def test_write_pinned_group_not_folded():
    # A write whose result the producer pinned (RETENTION_PIN) must keep its
    # new_string verbatim — same protection results get.
    msgs = make_writes(5)
    msgs[1].add_metadata(RETENTION, RETENTION_PIN)  # result of w-0
    t = _transcript(msgs)
    out = _fold(t, write_fold_names=WRITE_FOLD_NAMES)
    # w-0's call new_string survives (its group is pinned); the transcript pins
    # the whole group, so its assistant call is skipped by foldable_writes.
    strings = _new_strings(out.transcript)
    assert strings[0] == _BIG_BODY
