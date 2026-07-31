#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.compress.base``.

The fail-safe scaffolding: exceptions collapse to unchanged, the grow-guard
rejects bloating "compression", and ANSI stripping is clean.
"""
from __future__ import annotations

from mote.runtime.terminal_ansi import strip_ansi
from mote.runtime.tools.compress.base import CompressionResult, applied, safe_compress, unchanged


class TestStripAnsi:
    def test_removes_color_codes(self):
        colored = "\x1b[31mred\x1b[0m and \x1b[1mbold\x1b[0m"
        assert strip_ansi(colored) == "red and bold"

    def test_plain_text_untouched(self):
        assert strip_ansi("no escapes here") == "no escapes here"


class TestCompressionResult:
    def test_saved_chars_when_applied(self):
        r = applied("x" * 100, "y" * 30, "lbl")
        assert r.saved_chars == 70
        assert r.applied is True
        assert r.label == "lbl"

    def test_saved_chars_zero_when_not_applied(self):
        r = unchanged("x" * 100)
        assert r.applied is False
        assert r.saved_chars == 0
        assert r.text == "x" * 100


class TestSafeCompress:
    def test_exception_returns_unchanged(self):
        def boom(output, *, argv):
            raise RuntimeError("kaboom")

        r = safe_compress(boom, "original text", [])
        assert r.applied is False
        assert r.text == "original text"

    def test_non_result_returns_unchanged(self):
        def bad(output, *, argv):
            return "not a CompressionResult"

        r = safe_compress(bad, "original", [])
        assert r.applied is False
        assert r.text == "original"

    def test_grow_guard_rejects_larger_output(self):
        def grow(output, *, argv):
            return applied(output, output + " padding added", "grower")

        r = safe_compress(grow, "small", [])
        assert r.applied is False
        assert r.text == "small"
        assert r.label == "grower"  # label preserved for observability

    def test_genuine_shrink_passes_through(self):
        def shrink(output, *, argv):
            return applied(output, "tiny", "shrinker")

        r = safe_compress(shrink, "a much longer original string", [])
        assert r.applied is True
        assert r.text == "tiny"
        assert isinstance(r, CompressionResult)
