#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.contracts.text.whitespace`."""
from __future__ import annotations

from mote.contracts.text import collapse_whitespace


class TestCollapseWhitespace:
    def test_runs_collapse_to_single_space(self):
        assert collapse_whitespace("a   b") == "a b"

    def test_newlines_and_tabs_flatten(self):
        assert collapse_whitespace("a  b\n c\t d") == "a b c d"

    def test_leading_trailing_stripped(self):
        assert collapse_whitespace("  hi  ") == "hi"

    def test_empty_stays_empty(self):
        assert collapse_whitespace("") == ""

    def test_whitespace_only_stays_empty(self):
        assert collapse_whitespace("   \n\t ") == ""
