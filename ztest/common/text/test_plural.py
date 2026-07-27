#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.contracts.text.plural`."""
from __future__ import annotations

import pytest

from mote.contracts.text import count_noun, plural, verb_agree


class TestPlural:
    def test_singular_for_one(self):
        assert plural("file", 1) == "file"

    def test_plural_for_zero(self):
        assert plural("file", 0) == "files"

    def test_plural_for_many(self):
        assert plural("file", 3) == "files"

    def test_plural_for_negative(self):
        # Negatives are not "one", so they pluralise like any other non-1 count.
        assert plural("file", -2) == "files"

    @pytest.mark.parametrize(
        "noun,n,expected",
        [
            ("line", 1, "line"),
            ("line", 2, "lines"),
            ("occurrence", 1, "occurrence"),
            ("occurrence", 5, "occurrences"),
            ("element", 0, "elements"),
        ],
    )
    def test_matrix(self, noun, n, expected):
        assert plural(noun, n) == expected


class TestCountNoun:
    def test_one(self):
        assert count_noun(1, "file") == "1 file"

    def test_zero(self):
        assert count_noun(0, "file") == "0 files"

    def test_many(self):
        assert count_noun(4, "occurrence") == "4 occurrences"


class TestVerbAgree:
    def test_singular_for_one(self):
        assert verb_agree(1, "was", "were") == "was"

    def test_plural_for_zero(self):
        assert verb_agree(0, "is", "are") == "are"

    def test_plural_for_many(self):
        assert verb_agree(3, "was", "were") == "were"
