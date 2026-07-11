#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the fuzzy line matcher (``_apply_patch.seek.seek_sequence``).

Mirrors codex's ``seek_sequence`` unit tests: exact / rstrip / trim / Unicode
normalisation passes, the pattern-too-long guard, the empty-pattern no-op, and
the end-of-file bias.
"""
from __future__ import annotations

from mote.executor.dependency._apply_patch.seek import seek_sequence


class TestStrictness:
    def test_exact_match_finds_sequence(self):
        lines = ["foo", "bar", "baz"]
        assert seek_sequence(lines, ["bar", "baz"], 0, False) == 1

    def test_rstrip_match_ignores_trailing_whitespace(self):
        lines = ["foo   ", "bar\t\t"]
        assert seek_sequence(lines, ["foo", "bar"], 0, False) == 0

    def test_trim_match_ignores_leading_and_trailing_whitespace(self):
        lines = ["    foo   ", "   bar\t"]
        assert seek_sequence(lines, ["foo", "bar"], 0, False) == 0

    def test_unicode_normalize_match(self):
        # File has a typographic dash + curly quote; pattern uses ASCII.
        lines = ["x \u2014 y", "say \u2018hi\u2019"]
        assert seek_sequence(lines, ["x - y", "say 'hi'"], 0, False) == 0

    def test_unicode_nbsp_normalized(self):
        lines = ["a\u00a0b"]
        assert seek_sequence(lines, ["a b"], 0, False) == 0


class TestEdgeCases:
    def test_pattern_longer_than_input_returns_none(self):
        assert seek_sequence(["just one line"], ["too", "many", "lines"], 0, False) is None

    def test_empty_pattern_returns_start(self):
        assert seek_sequence(["a", "b"], [], 1, False) == 1

    def test_no_match_returns_none(self):
        assert seek_sequence(["a", "b", "c"], ["x"], 0, False) is None

    def test_start_offset_skips_earlier_match(self):
        lines = ["dup", "mid", "dup"]
        assert seek_sequence(lines, ["dup"], 1, False) == 2

    def test_eof_bias_prefers_tail(self):
        lines = ["x", "x", "x"]
        # eof=True biases the search to the last possible position.
        assert seek_sequence(lines, ["x"], 0, True) == 2

    def test_eof_falls_back_when_tail_mismatch(self):
        # Pattern only matches at index 0; eof tail position (1) doesn't match,
        # but the exact pass scans forward from the tail start and won't find it
        # before the tail — so this confirms eof anchors at the tail only.
        lines = ["match", "nope"]
        assert seek_sequence(lines, ["match"], 0, True) is None
