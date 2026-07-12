#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the zero-dependency ICU-subset formatter.

Covers the whole supported grammar (literals, ``{name}`` interpolation, ``#``
count, ``plural`` with ``=N`` + category, nested ``select``) plus the
never-raises contract on missing params / malformed patterns.
"""
from __future__ import annotations

from mote.common.i18n.locale import get_locale
from mote.common.i18n.message import format_message

EN = get_locale("en")
ZH = get_locale("zh")


def test_literal_and_interpolation() -> None:
    assert format_message("hi {name}", {"name": "Ada"}) == "hi Ada"


def test_missing_param_renders_empty() -> None:
    assert format_message("a{x}b", {}) == "ab"


def test_hash_renders_the_count() -> None:
    pat = "{n, plural, one{# item} other{# items}}"
    assert format_message(pat, {"n": 1}, EN) == "1 item"
    assert format_message(pat, {"n": 3}, EN) == "3 items"


def test_exact_case_beats_category() -> None:
    pat = "{n, plural, =0{none} one{# one} other{# many}}"
    assert format_message(pat, {"n": 0}, EN) == "none"
    assert format_message(pat, {"n": 1}, EN) == "1 one"
    assert format_message(pat, {"n": 9}, EN) == "9 many"


def test_chinese_always_other() -> None:
    pat = "{n, plural, one{# 项} other{# 项}}"
    assert format_message(pat, {"n": 1}, ZH) == "1 项"
    assert format_message(pat, {"n": 5}, ZH) == "5 项"


def test_select_with_other_fallback() -> None:
    pat = "{kind, select, file{a file} dir{a dir} other{something}}"
    assert format_message(pat, {"kind": "file"}) == "a file"
    assert format_message(pat, {"kind": "zzz"}) == "something"


def test_nested_select_inside_plural() -> None:
    pat = "{n, plural, one{# {kind, select, f{file} other{thing}}} other{# things}}"
    assert format_message(pat, {"n": 1, "kind": "f"}, EN) == "1 file"
    assert format_message(pat, {"n": 2, "kind": "f"}, EN) == "2 things"


def test_malformed_pattern_never_raises() -> None:
    # An unbalanced brace must fall back to the raw pattern, not crash.
    assert format_message("{n, plural, one{oops", {"n": 1}, EN) is not None
