#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.context.history.budget`` — the window-aware threshold math.

Covers token counting, the window/buffer/threshold helpers (incl. the
window-scaled buffer tiers) and ``evaluate`` (the warning / error / autocompact /
blocking state, ``tokens_freed`` subtraction, the autocompact-disabled branch and
``percent_left``).

The buffer-tier and ``evaluate`` tests patch a single helper each
(``effective_window`` / ``count_tokens``) so the assertions read against exact,
hand-computed thresholds rather than the machine's real token counts.
"""

from __future__ import annotations

import pytest

from mote.contracts.conversation import TokenState, UserMessage
from mote.contracts.conversation.constants import (
    AUTOCOMPACT_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    WARNING_THRESHOLD_BUFFER_TOKENS,
)
from mote.runtime.context.history import budget

UNKNOWN_MODEL = "definitely-not-a-real-model"


def test_count_tokens_empty_is_zero():
    assert budget.count_tokens([], "gpt-4") == 0


def test_count_tokens_grows_with_content():
    short = budget.count_tokens([UserMessage(content="hi")], "gpt-4")
    long = budget.count_tokens([UserMessage(content="hello world " * 100)], "gpt-4")
    assert long > short > 0


def test_count_tokens_drops_tool_calls_envelope():
    """A native assistant turn's ``tool_calls`` list must not crash the counter."""
    from mote.contracts.conversation import AIMessage
    from mote.contracts.conversation.fields import TOOL_CALLS

    m = AIMessage(content="calling")
    m.add_metadata(TOOL_CALLS, [{"id": "1", "name": "Read", "args": {"path": "x"}}])
    # Should count the text content, not try to encode the tool_calls list.
    assert budget.count_tokens([m], "gpt-4") > 0


def test_context_window_known_and_unknown():
    assert budget.context_window("gpt-4", context_tokens=8192) == 8192
    assert budget.context_window(UNKNOWN_MODEL) == MODEL_CONTEXT_WINDOW_DEFAULT


def test_effective_window_reserves_summary_output():
    assert budget.effective_window(UNKNOWN_MODEL) == (MODEL_CONTEXT_WINDOW_DEFAULT - MAX_OUTPUT_TOKENS_FOR_SUMMARY)


def test_effective_window_custom_reserve_override():
    assert budget.effective_window(UNKNOWN_MODEL, summary_reserve=0) == MODEL_CONTEXT_WINDOW_DEFAULT
    assert budget.effective_window(UNKNOWN_MODEL, summary_reserve=1000) == MODEL_CONTEXT_WINDOW_DEFAULT - 1000


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
    monkeypatch.setattr(budget, "effective_window", lambda model, **kw: window)
    assert budget.autocompact_buffer("m") == expected


def test_autocompact_threshold_is_window_minus_buffer():
    expected = budget.effective_window(UNKNOWN_MODEL) - budget.autocompact_buffer(UNKNOWN_MODEL)
    assert budget.autocompact_threshold(UNKNOWN_MODEL) == expected


# ---------------------------------------------------------------------------
# evaluate(): drive token_count via a patched count_tokens against the
# UNKNOWN_MODEL window (effective 180k, threshold 167k).
# ---------------------------------------------------------------------------


def _patch_count(monkeypatch, value: int):
    monkeypatch.setattr(budget, "count_tokens", lambda msgs, model: value)


def test_evaluate_below_all_thresholds(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)  # 167_000
    _patch_count(monkeypatch, threshold - WARNING_THRESHOLD_BUFFER_TOKENS - 1)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert isinstance(state, TokenState)
    assert not state.above_warning
    assert not state.above_error
    assert not state.above_autocompact
    assert not state.at_blocking_limit
    assert not state.should_autocompact


def test_evaluate_warning_and_error_boundary(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    # warning_threshold == error_threshold (both buffers are 20k by default).
    assert WARNING_THRESHOLD_BUFFER_TOKENS == ERROR_THRESHOLD_BUFFER_TOKENS
    _patch_count(monkeypatch, threshold - WARNING_THRESHOLD_BUFFER_TOKENS)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.above_warning
    assert state.above_error
    assert not state.above_autocompact


def test_evaluate_at_autocompact_and_blocking(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.above_autocompact
    assert state.should_autocompact
    # blocking limit sits below the threshold, so it is also crossed.
    assert MANUAL_COMPACT_BUFFER_TOKENS < WARNING_THRESHOLD_BUFFER_TOKENS
    assert state.at_blocking_limit


def test_evaluate_tokens_freed_lowers_count(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold)
    # Freeing 1 token drops us just under the autocompact threshold.
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, tokens_freed=1)
    assert state.token_count == threshold - 1
    assert not state.above_autocompact


def test_evaluate_tokens_freed_clamped_at_zero(monkeypatch):
    _patch_count(monkeypatch, 100)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, tokens_freed=10_000)
    assert state.token_count == 0


def test_evaluate_disabled_uses_full_effective_window(monkeypatch):
    window = budget.effective_window(UNKNOWN_MODEL)  # 180_000
    ac_threshold = budget.autocompact_threshold(UNKNOWN_MODEL)  # 167_000
    # Pick a count above the *enabled* warning threshold (ac_threshold - 20k =
    # 147k) but below the *disabled* one (window - 20k = 160k). The warning flag
    # then differs by branch while above_autocompact (always vs ac_threshold)
    # stays the same.
    enabled_warning = ac_threshold - WARNING_THRESHOLD_BUFFER_TOKENS  # 147_000
    disabled_warning = window - WARNING_THRESHOLD_BUFFER_TOKENS  # 160_000
    count = (enabled_warning + disabled_warning) // 2  # 153_500
    _patch_count(monkeypatch, count)
    enabled = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, autocompact_enabled=True)
    disabled = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, autocompact_enabled=False)
    assert enabled.above_warning  # crossed the smaller (autocompact) threshold
    assert not disabled.above_warning  # measured against the bigger window
    # above_autocompact reads the fixed ac_threshold in both branches.
    assert enabled.above_autocompact == disabled.above_autocompact is False
    assert disabled.autocompact_threshold == ac_threshold


def test_evaluate_percent_left(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold // 2)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.percent_left == 50


def test_evaluate_percent_left_floored_at_zero(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    _patch_count(monkeypatch, threshold * 3)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL)
    assert state.percent_left == 0


# ---------------------------------------------------------------------------
# observed_tokens (server truth) + TokenAccountant
# ---------------------------------------------------------------------------


def test_evaluate_prefers_observed_over_estimate(monkeypatch):
    threshold = budget.autocompact_threshold(UNKNOWN_MODEL)
    # tiktoken would say "tiny", but the server billed us over the threshold.
    _patch_count(monkeypatch, 5)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, observed_tokens=threshold + 1)
    assert state.token_count == threshold + 1
    assert state.above_autocompact is True


def test_evaluate_falls_back_to_estimate_when_observed_zero(monkeypatch):
    _patch_count(monkeypatch, 123)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, observed_tokens=0)
    assert state.token_count == 123


def test_evaluate_falls_back_when_observed_none(monkeypatch):
    _patch_count(monkeypatch, 77)
    state = budget.evaluate([UserMessage(content="x")], UNKNOWN_MODEL, observed_tokens=None)
    assert state.token_count == 77


class _Usage:
    def __init__(self, total):
        self.total_tokens = total

    def is_zero(self):
        return self.total_tokens == 0


class _CostManager:
    def __init__(self, total):
        self.last_usage = _Usage(total)


class _LLM:
    def __init__(self, total):
        self.cost_manager = _CostManager(total)


def test_accountant_reads_server_total():
    acc = budget.TokenAccountant(_LLM(4321))
    assert acc.observed() == 4321


def test_accountant_none_when_zero_usage():
    acc = budget.TokenAccountant(_LLM(0))
    assert acc.observed() is None


def test_accountant_none_without_llm():
    assert budget.TokenAccountant(None).observed() is None


def test_accountant_none_without_cost_manager():
    class Bare:
        pass

    assert budget.TokenAccountant(Bare()).observed() is None
