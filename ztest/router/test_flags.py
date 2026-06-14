#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.router.flags (the ergonomic, keyword-driven flag wrapper)."""
from __future__ import annotations

from metagpt.router.flags import (
    HEAVY_CONTEXT_TOKENS,
    LONG_CONTEXT_CHAR_THRESHOLD,
    RoutingFlags,
    compute_flags,
    merge_request_flags,
)


class TestComputeFlags:
    def test_empty_text_all_false(self):
        f = compute_flags("")
        assert f == RoutingFlags()
        assert not any([f.high_risk, f.long_context, f.debug, f.repo_arch, f.strict_format])

    def test_none_text_is_safe(self):
        f = compute_flags(None)  # type: ignore[arg-type]
        assert f.high_risk is False

    def test_high_risk_zh(self):
        assert compute_flags("请帮我执行生产环境的回滚").high_risk is True

    def test_high_risk_en(self):
        assert compute_flags("we need to deploy and rollback the migration").high_risk is True

    def test_debug_keyword(self):
        assert compute_flags("there is a bug causing an exception").debug is True

    def test_debug_pattern(self):
        assert compute_flags("Traceback (most recent call last):").debug is True

    def test_repo_arch(self):
        assert compute_flags("refactor the architecture of the monorepo").repo_arch is True

    def test_strict_format(self):
        assert compute_flags("only return JSON, no explanation 只返回").strict_format is True

    def test_long_context_by_length(self):
        assert compute_flags("x" * LONG_CONTEXT_CHAR_THRESHOLD).long_context is True

    def test_long_context_by_file_refs(self):
        # two file references trip the file_ref threshold (>= 2)
        text = "see src/foo/bar.py and lib/baz/qux.py for details"
        assert compute_flags(text).long_context is True

    def test_long_context_forced_by_context_tokens(self):
        f = compute_flags("short", context_tokens_est=HEAVY_CONTEXT_TOKENS + 1)
        assert f.long_context is True

    def test_context_tokens_below_threshold(self):
        f = compute_flags("short", context_tokens_est=HEAVY_CONTEXT_TOKENS)
        assert f.long_context is False


class TestRoutingFlagsAnyOf:
    def test_any_of_true(self):
        f = RoutingFlags(debug=True)
        assert f.any_of(["high_risk", "debug"]) is True

    def test_any_of_false(self):
        f = RoutingFlags(debug=True)
        assert f.any_of(["high_risk", "long_context"]) is False

    def test_any_of_unknown_name(self):
        assert RoutingFlags().any_of(["does_not_exist"]) is False


class TestMergeRequestFlags:
    def test_no_request_flags_returns_same(self):
        f = RoutingFlags(debug=True)
        assert merge_request_flags(f, None) is f
        assert merge_request_flags(f, set()) is f

    def test_escalates_only(self):
        f = RoutingFlags(debug=True)
        merged = merge_request_flags(f, {"high_risk", "long_context"})
        assert merged.high_risk is True
        assert merged.long_context is True
        assert merged.debug is True  # preserved

    def test_cannot_clear_existing(self):
        f = RoutingFlags(debug=True)
        # request flags omit debug, but it stays set (escalate-only).
        merged = merge_request_flags(f, {"high_risk"})
        assert merged.debug is True
