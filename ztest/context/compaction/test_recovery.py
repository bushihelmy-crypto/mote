#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`RecoveryContextReducer` — the HARD fold+drop wire adapter.

Recovery fires *inside* an in-flight LLM call, so this reducer runs only the
non-LLM reducers (fold + head-drop) over the ``Message.to_dict()`` wire dicts
``BaseLLM`` holds. These tests assert the wire<->Message bridge round-trips
correctly (tool metadata preserved, pristine dicts re-emitted, in-place folds
reflected) and that "nothing freed" surfaces as ``None`` so the recovery loop
does not spin on an identical payload.
"""
from __future__ import annotations

import asyncio

from mote.contracts.conversation.fields import TOOL_CALL_ID, TOOL_CALLS
from mote.runtime.context.compaction.recovery import RecoveryContextReducer, _message_to_wire, _wire_to_message
from mote.runtime.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.runtime.context.compaction.transcript import Transcript
from mote.ztest.model_fakes import model_route


def _run(coro):
    return asyncio.run(coro)


class RecordingReducer:
    """A fake reducer that records the request it saw and optionally rewrites."""

    def __init__(self, cost, *, new_transcript=None, strategy="fake"):
        self.cost = cost
        self.request = None
        self._new = new_transcript
        self._strategy = strategy

    async def reduce(self, transcript, request):
        self.request = request
        out = self._new if self._new is not None else transcript
        return ReductionOutcome(out, changed=self._new is not None, strategy=self._strategy)


# ---------------------------------------------------------------------------
# wire <-> Message bridge
# ---------------------------------------------------------------------------


def test_wire_to_message_tool_result_carries_call_id():
    d = {"role": "tool", "tool_call_id": "c1", "content": "result body"}
    m = _wire_to_message(d)
    assert m.role == "tool"
    assert m.metadata[TOOL_CALL_ID] == "c1"
    assert m.content == "result body"


def test_wire_to_message_tool_call_parses_function_envelope():
    d = {
        "role": "assistant",
        "content": "calling",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"x"}'}}],
    }
    m = _wire_to_message(d)
    calls = m.metadata[TOOL_CALLS]
    assert calls == [{"id": "c1", "name": "Read", "args": '{"path":"x"}'}]


def test_wire_to_message_flattens_list_content_for_counting():
    d = {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]}
    m = _wire_to_message(d)
    assert m.content == "hello world"


def test_kept_plain_message_emits_pristine_original():
    # A multimodal user turn must round-trip byte-for-byte (no lossy flatten).
    d = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}
    m = _wire_to_message(d)
    assert _message_to_wire(m) is d


def test_tool_result_round_trips_via_to_dict():
    d = {"role": "tool", "tool_call_id": "c1", "content": "body"}
    m = _wire_to_message(d)
    out = _message_to_wire(m)
    assert out["role"] == "tool"
    assert out["tool_call_id"] == "c1"


def test_mutated_string_content_is_reflected_not_discarded():
    # A string-bodied message keeps NO pristine original, so a reducer's in-place
    # rewrite (e.g. spill replacing a runaway body with a pointer) is emitted via
    # to_dict — never silently discarded by a stale original.
    d = {"role": "assistant", "content": "R" * 5000}
    m = _wire_to_message(d)
    m.content = "<persisted-output>…pointer…</persisted-output>"
    out = _message_to_wire(m)
    assert out["content"] == "<persisted-output>…pointer…</persisted-output>"


def test_mutated_tool_call_args_are_reflected():
    # A tool_calls message is faithful via to_dict, so a rewritten args string
    # (spill of a giant blob) rides back out on the wire.
    d = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": "B" * 5000}}],
    }
    m = _wire_to_message(d)
    m.metadata[TOOL_CALLS][0]["args"] = "<persisted-output>…args…</persisted-output>"
    out = _message_to_wire(m)
    assert out["tool_calls"][0]["function"]["arguments"] == "<persisted-output>…args…</persisted-output>"


def test_cache_intent_survives_round_trip():
    # The declarative cache-intent hint is restored on reconstruction so a kept
    # string message re-emits it (to_dict path), not just multimodal originals.
    d = {"role": "user", "content": "hi", "_cache_intent": "ephemeral"}
    m = _wire_to_message(d)
    out = _message_to_wire(m)
    assert out["_cache_intent"] == "ephemeral"


def test_tool_references_survive_tool_result_round_trip():
    d = {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "discovered",
        "_tool_references": ["Read", "Write"],
    }
    m = _wire_to_message(d)
    out = _message_to_wire(m)
    assert out["_tool_references"] == ["Read", "Write"]


# ---------------------------------------------------------------------------
# reduce()
# ---------------------------------------------------------------------------


def test_reduce_empty_returns_none():
    reducer = RecoveryContextReducer([], model="gpt-4")
    assert _run(reducer.reduce([], target_tokens=100)) is None


def test_reduce_runs_hard_reactive_request():
    from mote.runtime.context.compaction.request import ReductionReason, Urgency

    rec = RecordingReducer(ReducerCost.FREE)
    reducer = RecoveryContextReducer([rec], model="gpt-4")
    _run(reducer.reduce([{"role": "user", "content": "hi"}], target_tokens=1))
    assert rec.request is not None
    assert rec.request.urgency == Urgency.HARD
    assert rec.request.reason == ReductionReason.REACTIVE
    assert rec.request.target_tokens == 1


def test_reduce_returns_none_when_nothing_changed():
    # A no-op reducer leaves the transcript unchanged → None (avoid re-issue spin).
    rec = RecordingReducer(ReducerCost.FREE)
    reducer = RecoveryContextReducer([rec], model="gpt-4")
    out = _run(reducer.reduce([{"role": "user", "content": "hi"}], target_tokens=1))
    assert out is None


def test_reduce_emits_wire_dicts_when_changed():
    smaller = Transcript.from_messages([_wire_to_message({"role": "user", "content": "x"})])
    rec = RecordingReducer(ReducerCost.DESTRUCTIVE, new_transcript=smaller)
    reducer = RecoveryContextReducer([rec], model="gpt-4")
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    out = _run(reducer.reduce(messages, target_tokens=1))
    assert out is not None
    assert all(isinstance(d, dict) and "role" in d for d in out)


# ---------------------------------------------------------------------------
# fold → summarize → drop escalation (the real reducers)
# ---------------------------------------------------------------------------

from mote.contracts.conversation import ContextManagerConfig  # noqa: E402
from mote.contracts.conversation.constants import HEAD_DROPPED_MESSAGE  # noqa: E402
from mote.runtime.context.compaction.reducers.drop import HeadDropReducer  # noqa: E402
from mote.runtime.context.compaction.reducers.fold import FoldReducer  # noqa: E402
from mote.runtime.context.compaction.reducers.summarize import SummarizeReducer  # noqa: E402

from ..conftest import FakeLLM, text_msg  # noqa: E402


def _plain_wire() -> list[dict]:
    """A bulky-head + tiny-tail plain-text history as wire dicts (no tool bodies,
    so fold can't shrink it — only summarize condensing the head, or a raw drop,
    can bring it under target). The tiny tail keeps ``summary + tail`` small
    enough to meet the target so summarize alone suffices."""
    msgs = [text_msg(("word " * 60) + f" {i}") for i in range(7)]
    msgs.append(text_msg("bye"))
    return [m.to_dict() for m in msgs]


def _has_head_drop_marker(messages: list[dict]) -> bool:
    return any(HEAD_DROPPED_MESSAGE in (d.get("content") or "") for d in messages)


def _pairing_valid(messages: list[dict]) -> bool:
    """Every tool_result wire dict must follow the tool_call it answers."""
    seen: set = set()
    for d in messages:
        for c in d.get("tool_calls") or []:
            seen.add(c.get("id"))
        cid = d.get("tool_call_id")
        if cid is not None and cid not in seen:
            return False
    return True


def test_reduce_escalates_to_summarize():
    # Fold can't touch plain text; summarize condenses the head to a short
    # summary that fits, so the destructive drop never fires.
    cfg = ContextManagerConfig(keep_tail_messages=1, keep_tail_tokens=1)
    fold = FoldReducer(cfg, model="gpt-4")
    summarize = SummarizeReducer(model_route(FakeLLM(summary="CONDENSED")), cfg, model="gpt-4")
    drop = HeadDropReducer(cfg, model="gpt-4")
    reducer = RecoveryContextReducer([fold, summarize, drop], model="gpt-4")

    out = _run(reducer.reduce(_plain_wire(), target_tokens=120))

    assert out is not None
    assert any("CONDENSED" in (d.get("content") or "") for d in out)  # summary present
    assert not _has_head_drop_marker(out)  # summarize met target → no destructive drop
    assert _pairing_valid(out)


def test_reduce_falls_back_to_drop_when_summarize_noops():
    # Summarize disabled (no-op) and still over target → HeadDropReducer fires:
    # the destructive floor is preserved when summarize can't free enough.
    cfg = ContextManagerConfig(enable_autocompact=False, keep_tail_messages=1, keep_tail_tokens=1)
    fold = FoldReducer(cfg, model="gpt-4")
    summarize = SummarizeReducer(model_route(FakeLLM(summary="CONDENSED")), cfg, model="gpt-4")
    drop = HeadDropReducer(cfg, model="gpt-4")
    reducer = RecoveryContextReducer([fold, summarize, drop], model="gpt-4")

    out = _run(reducer.reduce(_plain_wire(), target_tokens=120))

    assert out is not None
    assert not any("CONDENSED" in (d.get("content") or "") for d in out)  # summarize no-op'd
    assert _has_head_drop_marker(out)  # drop fallback fired
    assert _pairing_valid(out)
