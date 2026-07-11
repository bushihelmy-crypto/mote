#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the ``compress_output`` entry point (escape valves)."""
from __future__ import annotations

from mote.executor.compress import compress_output

# A genuinely compressible pytest blob, comfortably above the min floor.
_PYTEST = (
    "============================= test session starts =============================\n"
    "collected 50 items\n"
    + "".join(f"tests/t{i}.py .......... [ {i * 2}%]\n" for i in range(50))
    + "========================= 50 passed in 1.00s =========================\n"
)


class TestEscapeValves:
    def test_below_min_chars_unchanged(self):
        out = "git status\n" + "x" * 100
        r = compress_output("git status", out, min_chars=2000, max_input_chars=2_000_000)
        assert r.applied is False
        assert r.text == out

    def test_above_max_chars_unchanged(self):
        big = _PYTEST * 500
        r = compress_output("pytest", big, min_chars=2000, max_input_chars=1000)
        assert r.applied is False
        assert r.text == big

    def test_empty_command_unchanged(self):
        r = compress_output("", _PYTEST, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is False

    def test_empty_output_unchanged(self):
        r = compress_output("pytest", "", min_chars=1, max_input_chars=2_000_000)
        assert r.applied is False

    def test_unknown_prefix_unchanged(self):
        out = "some output\n" * 500
        r = compress_output("ls -la", out, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is False
        assert r.text == out


class TestApplied:
    def test_known_command_compressed(self):
        r = compress_output("pytest tests/", _PYTEST, min_chars=100, max_input_chars=2_000_000)
        assert r.applied is True
        assert r.compressed_chars < r.original_chars

    def test_ansi_preserved_when_declined(self):
        # An unknown command with ANSI: declined -> original (ANSI-bearing) kept.
        colored = "\x1b[31m" + ("noise line\n" * 500) + "\x1b[0m"
        r = compress_output("ls", colored, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is False
        assert "\x1b[31m" in r.text
