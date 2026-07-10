#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.compress.ruff.RuffCompressor``."""
from __future__ import annotations

from metagpt.executor.compress.ruff import RuffCompressor

OUTPUT = (
    "\n".join(f"src/mod{i}.py:{i}:1: F401 `os` imported but unused" for i in range(20))
    + "\n"
    + "\n".join(f"src/mod{i}.py:{i}:80: E501 line too long ({80 + i} > 79)" for i in range(5))
    + "\n"
    "Found 25 errors.\n"
    "[*] 20 fixable with the `--fix` option.\n"
)


class TestRuffCompressor:
    def test_applies_and_shrinks(self):
        r = RuffCompressor().compress(OUTPUT, argv=["ruff", "check"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert r.label == "ruff"

    def test_grouped_counts(self):
        r = RuffCompressor().compress(OUTPUT, argv=["ruff", "check"])
        assert "F401: 20 occurrence(s)" in r.text
        assert "E501: 5 occurrence(s)" in r.text

    def test_first_locations_kept_and_capped(self):
        r = RuffCompressor().compress(OUTPUT, argv=["ruff", "check"])
        # First 3 F401 locations shown, rest summarised.
        assert "src/mod0.py:0:1: `os` imported but unused" in r.text
        assert "... and 17 more" in r.text

    def test_footer_preserved(self):
        r = RuffCompressor().compress(OUTPUT, argv=["ruff", "check"])
        assert "Found 25 errors." in r.text
        assert "fixable" in r.text

    def test_unparsable_output_unchanged(self):
        noise = "some random text\nthat is not lint findings\n" * 100
        r = RuffCompressor().compress(noise, argv=["ruff"])
        assert r.applied is False
        assert r.text == noise
