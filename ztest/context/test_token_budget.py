#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.context.token_budget`` — the window-aware threshold math.

Covers token counting, the window/buffer/threshold helpers (incl. CC's
window-scaled buffer tiers) and ``evaluate`` (the warning / error / autocompact /
blocking state, ``tokens_freed`` subtraction, the autocompact-disabled branch and
``percent_left``).

The buffer-tier and ``evaluate`` tests patch a single helper each
(``effective_window`` / ``count_tokens``) so the assertions read against exact,
hand-computed thresholds rather than the machine's real token counts.
"""
from __future__ import annotations

import pytest

from metagpt.common.const.context import (
    AUTOCOMPACT_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    WARNING_THRESHOLD_BUFFER_TOKENS,
)
from metagpt.common.schema import TokenState, UserMessage
from metagpt.common.utils.token_counter import TOKEN_MAX
from context import token_budget

UNKNOWN_MODEL = "definitely-not-a-real-model"


def test_count_tokens_empty_is_zero():
    assert token_budget.count_tokens([], "gpt-4") == 0


def test_count_tokens_grows_with_content():
    short = token_budget.count_tokens([UserMessage(content="hi")], "gpt-4")
    long = token_budget.count_tokens([UserMessage(content="hello world " * 100)], "gpt-4")
    assert long > short > 0


def test_count_tokens_accepts_plain_dicts():
    """A built request is already wire-format dicts; the counter must handle both."""
    n = token_budget.count_tokens([{"role": "user", "content": "hello there"}], "gpt-4")
    assert n > 0


def test_count_tokens_drops_tool_calls_envelope():
    """A native assistant turn's ``tool_calls`` list must not crash the counter."""
    from metagpt.common.const import TOOL_CALLS
    from metagpt.common.schema import AIMessage

    m = AIMessage(content="calling")
    m.add_metadata(TOOL_CALLS, [{"id": "1", "name": "Read", "args": {"path": "x"}}])
    # Should count the text content, not try to encode the tool_calls list.
    assert token_budget.count_tokens([m], "gpt-4") > 0


def test_context_window_known_and_unknown():
    assert token_budget.context_window("gpt-4") == TOKEN_MAX["gpt-4"]
    assert token_budget.context_window(UNKNOWN_MODEL) == MODEL_CONTEXT_WINDOW_DEFAULT


def test_effective_window_reserves_summary_output():
    assert token_budget.effective_window(UNKNOWN_MODEL) == (
        MODEL_CONTEXT_WINDOW_DEFAULT - MAX_OUTPUT_TOKENS_FOR_SUMMARY
    )


def test_effective_window_custom_reserve_override():
    assert token_budget.effective_window(UNKNOWN_MODEL, summary_reserve=0) == MODEL_CONTEXT_WINDOW_DEFAULT
    assert (
        token_budget.effective_window(UNKNOWN_MODEL, summary_reserve=1000)
        == MODEL_CONTEXT_WINDOW_DEFAULT - 1000
    )


@pytest.mark.parametrize(
    "window, expected",
    [
        (200_000, AUTOCOMPACT_BUFFER_TOKENS),  # < 400k → base buffer
        (399_999, AUTOCOMPACT_BUFFER_TOKENS),
        (400_000, 30_000),  # >= 400k tier
        (799_999, 30_000),
        (800_000, 50_000),  # >= 800k tier
        (1_000_000, 50_000),
    ],
)
def test_autocompact_buffer_tiers(monkeypatch, window, expected):
    monkeypatch.setattr(token_budget, "effective_window", lambda model, **kw: window)
    assert token_budget.autocompact_buffer("m") == expected


def test_autocompact_threshold_is_window_minus_buffer():
    expected = token_budget.effective_window(UNKNOWN_MODEL) - token_budget.autocompact_buffer(UNKNOWN_MODEL)
    assert token_budget.autocompact_threshold(UNKNOWN_MODEL) == expected


# ---------------------------------------------------------------------------
# evaluate(): drive token_count via a patched count_tokens against the
# UNKNOWN_MODEL window (effective 180k, threshold 167k).
# ---------------------------------------------------------------------------


def _patch_count(monkeypatch, value: int):
    monkeypatch.setattr(token_budget, "count_tokens", lambda msgs, model: value)


def test_evaluate_below_all_thresholds(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)  # 167_000
    _patch_count(monkeypatch, threshold - WARNING_THRESHOLD_BUFFER_TOKENS - 1)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert isinstance(state, TokenState)
    assert not state.above_warning
    assert not state.above_error
    assert not state.above_autocompact
    assert not state.at_blocking_limit
    assert not state.should_autocompact


def test_evaluate_warning_and_error_boundary(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)
    # warning_threshold == error_threshold (both buffers are 20k by default).
    assert WARNING_THRESHOLD_BUFFER_TOKENS == ERROR_THRESHOLD_BUFFER_TOKENS
    _patch_count(monkeypatch, threshold - WARNING_THRESHOLD_BUFFER_TOKENS)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.above_warning
    assert state.above_error
    assert not state.above_autocompact


def test_evaluate_at_autocompact_and_blocking(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.above_autocompact
    assert state.should_autocompact
    # blocking limit sits below the threshold, so it is also crossed.
    assert MANUAL_COMPACT_BUFFER_TOKENS < WARNING_THRESHOLD_BUFFER_TOKENS
    assert state.at_blocking_limit


def test_evaluate_tokens_freed_lowers_count(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold)
    # Freeing 1 token drops us just under the autocompact threshold.
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, tokens_freed=1)
    assert state.token_count == threshold - 1
    assert not state.above_autocompact


def test_evaluate_tokens_freed_clamped_at_zero(monkeypatch):
    _patch_count(monkeypatch, 100)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, tokens_freed=10_000)
    assert state.token_count == 0


def test_evaluate_disabled_uses_full_effective_window(monkeypatch):
    window = token_budget.effective_window(UNKNOWN_MODEL)  # 180_000
    ac_threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)  # 167_000
    # Pick a count above the *enabled* warning threshold (ac_threshold - 20k =
    # 147k) but below the *disabled* one (window - 20k = 160k). The warning flag
    # then differs by branch while above_autocompact (always vs ac_threshold)
    # stays the same.
    enabled_warning = ac_threshold - WARNING_THRESHOLD_BUFFER_TOKENS  # 147_000
    disabled_warning = window - WARNING_THRESHOLD_BUFFER_TOKENS  # 160_000
    count = (enabled_warning + disabled_warning) // 2  # 153_500
    _patch_count(monkeypatch, count)
    enabled = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, autocompact_enabled=True)
    disabled = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, autocompact_enabled=False)
    assert enabled.above_warning  # crossed the smaller (autocompact) threshold
    assert not disabled.above_warning  # measured against the bigger window
    # above_autocompact reads the fixed ac_threshold in both branches.
    assert enabled.above_autocompact == disabled.above_autocompact is False
    assert disabled.autocompact_threshold == ac_threshold


def test_evaluate_percent_left(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold // 2)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.percent_left == 50


def test_evaluate_percent_left_floored_at_zero(monkeypatch):
    threshold = token_budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold * 3)
    state = token_budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.percent_left == 0
