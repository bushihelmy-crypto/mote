#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for the :mod:`mote.runtime.text.elision` value object."""
from __future__ import annotations

import dataclasses

import pytest

from mote.runtime.text.elision import Elision, ElisionStrategy, ElisionUnit, cap_head, cap_head_tail


class TestCapHeadTail:
    def test_under_limit_returns_none(self):
        text = "short"
        out, el = cap_head_tail(text, 100)
        assert out == text
        assert el is None

    def test_at_limit_returns_none(self):
        text = "abcde"
        out, el = cap_head_tail(text, 5)
        assert out == text
        assert el is None

    def test_zero_limit_guard(self):
        text = "abcde"
        out, el = cap_head_tail(text, 0)
        assert out == text
        assert el is None

    def test_negative_limit_guard(self):
        text = "abcde"
        out, el = cap_head_tail(text, -3)
        assert out == text
        assert el is None

    def test_head_tail_roundtrip(self):
        text = "".join(str(i % 10) for i in range(100))  # 100 chars
        out, el = cap_head_tail(text, 20)
        assert el is not None
        # head = 10, tail = 10
        head = text[:10]
        tail = text[-10:]
        assert out == f"{head}\n[... 80 chars omitted ...]\n{tail}"
        assert el.omitted == 80
        assert el.total == 100
        assert el.unit is ElisionUnit.CHARS
        assert el.strategy is ElisionStrategy.HEAD_TAIL

    def test_odd_limit_split(self):
        text = "x" * 50
        out, el = cap_head_tail(text, 7)  # head=3, tail=4
        assert el is not None
        assert out == "xxx\n[... 43 chars omitted ...]\nxxxx"
        assert el.omitted == 43

    def test_bytes_unit_marker(self):
        text = "a" * 30
        out, el = cap_head_tail(text, 10, unit=ElisionUnit.BYTES)
        assert el is not None
        assert el.unit is ElisionUnit.BYTES
        assert "20 bytes omitted" in out

    def test_marker_has_no_outer_newlines_in_render(self):
        el = Elision(ElisionUnit.CHARS, 5, 10, ElisionStrategy.HEAD_TAIL)
        rendered = el.render_for_model()
        assert not rendered.startswith("\n")
        assert not rendered.endswith("\n")


class TestCapHead:
    def test_under_limit_returns_none(self):
        out, el = cap_head("short", 100)
        assert out == "short"
        assert el is None

    def test_zero_limit_guard(self):
        out, el = cap_head("abc", 0)
        assert out == "abc"
        assert el is None

    def test_head_slice_and_facts(self):
        text = "abcdefghij"  # 10
        out, el = cap_head(text, 4)
        assert out == "abcd"
        assert el is not None
        assert el.omitted == 6
        assert el.total == 10
        assert el.strategy is ElisionStrategy.HEAD
        assert el.unit is ElisionUnit.CHARS


class TestRenderForModel:
    def test_chars_default(self):
        el = Elision(ElisionUnit.CHARS, 42, 100, ElisionStrategy.HEAD_TAIL)
        assert el.render_for_model() == "[... 42 chars omitted ...]"

    def test_bytes_label(self):
        el = Elision(ElisionUnit.BYTES, 7, 20, ElisionStrategy.HEAD_TAIL)
        assert el.render_for_model() == "[... 7 bytes omitted ...]"

    def test_lines_with_noun_and_extra(self):
        el = Elision(ElisionUnit.LINES, 12, 30, ElisionStrategy.TAIL)
        rendered = el.render_for_model(noun="more changed lines", extra="(+3 -9 total)")
        assert rendered == "[... 12 more changed lines omitted (+3 -9 total) ...]"

    def test_tokens_label(self):
        el = Elision(ElisionUnit.TOKENS, 500, 2000, ElisionStrategy.HEAD)
        assert el.render_for_model() == "[... 500 tokens omitted ...]"

    def test_with_total(self):
        el = Elision(ElisionUnit.CHARS, 30, 100, ElisionStrategy.HEAD)
        assert el.render_for_model(with_total=True) == "[... 30 chars omitted of 100 total ...]"

    def test_format_count_and_with_total(self):
        el = Elision(ElisionUnit.BYTES, 2048, 4096, ElisionStrategy.HEAD)
        rendered = el.render_for_model(
            format_count=lambda n: f"{n}B",
            with_total=True,
        )
        assert rendered == "[... 2048B bytes omitted of 4096B total ...]"

    def test_noun_empty_string_override(self):
        el = Elision(ElisionUnit.CHARS, 5, 10, ElisionStrategy.HEAD)
        # noun="" is explicit override (not None) → collapses the filler word
        assert el.render_for_model(noun="") == "[... 5 omitted ...]"


class TestImmutability:
    def test_frozen(self):
        el = Elision(ElisionUnit.CHARS, 1, 2, ElisionStrategy.HEAD)
        with pytest.raises(dataclasses.FrozenInstanceError):
            el.omitted = 99  # type: ignore[misc]

    def test_unit_label(self):
        assert ElisionUnit.CHARS.label() == "chars"
        assert ElisionUnit.BYTES.label() == "bytes"
        assert ElisionUnit.LINES.label() == "lines"
        assert ElisionUnit.TOKENS.label() == "tokens"
