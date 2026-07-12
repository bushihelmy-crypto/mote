#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.context.microcompact`` — cheap in-place tool-result folding.

Microcompact never calls the LLM. It collects compactable tool-use ids, and once
more than ``trigger_threshold`` *active* results have accumulated, replaces the
content of all but the most-recent ``keep_recent`` with the placeholder. Tests
use a small config (trigger=2, keep_recent=1) so a handful of pairs exercise the
real gate, and assert: the no-op cases (disabled / below trigger), what gets
cleared, that the call/result pairing survives, idempotency, that non-compactable
tools are left alone, and the keep_recent floor of 1.
"""
from __future__ import annotations

from metagpt.common.const import TOOL_CALLS
from metagpt.common.schema import ContextManagerConfig, MicrocompactResult
from metagpt.context.microcompact import COMPACTABLE_TOOLS, microcompact

from .conftest import make_pairs, text_msg, tool_pair

PLACEHOLDER = "[Old tool result content cleared]"


def _cfg(trigger=2, keep=1, enable=True) -> ContextManagerConfig:
    return ContextManagerConfig(
        enable_microcompact=enable,
        microcompact_trigger_threshold=trigger,
        microcompact_keep_recent=keep,
    )


def _result_contents(messages):
    """Tool-result contents in order (messages carrying TOOL_CALL_ID)."""
    from metagpt.common.const import TOOL_CALL_ID

    return [m.content for m in messages if m.metadata.get(TOOL_CALL_ID)]


def test_disabled_is_noop():
    msgs = make_pairs(5)
    res = microcompact(msgs, _cfg(enable=False))
    assert isinstance(res, MicrocompactResult)
    assert not res.changed
    assert res.tokens_freed == 0
    assert all(c != PLACEHOLDER for c in _result_contents(msgs))


def test_below_trigger_is_noop():
    # 2 active results, trigger=2 → fires only when len(active) > trigger.
    msgs = make_pairs(2)
    res = microcompact(msgs, _cfg(trigger=2, keep=1))
    assert not res.changed
    assert all(c != PLACEHOLDER for c in _result_contents(msgs))


def test_folds_all_but_keep_recent():
    # 4 active, trigger=2, keep=1 → clear the first 3, keep the last.
    msgs = make_pairs(4)
    res = microcompact(msgs, _cfg(trigger=2, keep=1))
    assert res.changed
    assert res.tokens_freed > 0
    assert res.cleared_tool_use_ids == ["id-0", "id-1", "id-2"]
    contents = _result_contents(msgs)
    assert contents[:3] == [PLACEHOLDER] * 3
    assert contents[3] != PLACEHOLDER


def test_returns_same_list_object():
    """Folding is in place — the returned messages are the input list."""
    msgs = make_pairs(4)
    res = microcompact(msgs, _cfg())
    assert res.messages is msgs


def test_pairing_left_intact():
    """Only result *content* shrinks; the assistant tool-call turns are untouched."""
    msgs = make_pairs(4)
    microcompact(msgs, _cfg())
    call_turns = [m for m in msgs if m.metadata.get(TOOL_CALLS)]
    assert len(call_turns) == 4
    for m in call_turns:
        assert m.metadata[TOOL_CALLS][0]["name"] == "Read"


def test_idempotent_second_pass_noop():
    msgs = make_pairs(4)
    first = microcompact(msgs, _cfg(trigger=2, keep=1))
    assert first.changed
    # After clearing, only 1 active result remains (< trigger) → no further folds.
    second = microcompact(msgs, _cfg(trigger=2, keep=1))
    assert not second.changed
    assert second.tokens_freed == 0


def test_non_compactable_tools_ignored():
    # "End" is not in COMPACTABLE_TOOLS, so its result is never collected/cleared,
    # even though plenty of Read results pile up around it.
    assert "End" not in COMPACTABLE_TOOLS
    msgs = tool_pair("end-1", "End", "terminal output") + make_pairs(4, start=10)
    microcompact(msgs, _cfg(trigger=2, keep=1))
    # the End result keeps its content
    end_result = [m for m in msgs if m.metadata.get("tool_call_id") == "end-1"][0]
    assert end_result.content == "terminal output"


def test_keep_recent_floor_of_one():
    # keep_recent=0 must still keep at least one working result (CC floor).
    msgs = make_pairs(4)
    res = microcompact(msgs, _cfg(trigger=2, keep=0))
    assert res.changed
    contents = _result_contents(msgs)
    assert contents.count(PLACEHOLDER) == 3  # exactly one kept
    assert contents[-1] != PLACEHOLDER


def test_messages_without_tools_untouched():
    msgs = [text_msg("hi"), text_msg("there", role="assistant")]
    res = microcompact(msgs, _cfg())
    assert not res.changed
    assert [m.content for m in msgs] == ["hi", "there"]


def test_custom_compactable_set_override():
    # Default set won't fold a "MyTool" result; an override that includes it will.
    msgs = make_pairs(4, name="MyTool")
    assert not microcompact(msgs, _cfg(trigger=2, keep=1)).changed
    res = microcompact(msgs, _cfg(trigger=2, keep=1), compactable=frozenset({"MyTool"}))
    assert res.changed
