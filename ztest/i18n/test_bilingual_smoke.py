#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bilingual render smoke: the same event → correct zh *and* en display.

The i18n seam is only trustworthy if a representative slice of the display layer
actually swaps wording when the active locale changes. This renders the real
projector summaries + shared rich builders (read/grep/glob/edit/write/retry/
compaction/fold/group) under ``use_locale("zh")`` and ``use_locale("en")`` and
asserts the *exact* string for each — pinning the wording (incl. English CLDR
plural one/other) so a regression in either language fails here, not in the UI.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.cli.consumers.render.builders.core import (
    compaction_summary_text,
    conversation_compacted_text,
    fold_note_str,
    tool_group_summary_text,
)
from mote.cli.view.summaries import _summary_edit, _summary_read, _summary_search
from mote.common.i18n import use_locale

# A minimal Read body: right-justified ``N→`` numbered lines the summary counts.
_READ_1 = "     1\u2192only line\n"
_READ_3 = "     1\u2192a\n     2\u2192b\n     3\u2192c\n"


def _plain(text_obj) -> str:
    """The visible text of a rich ``Text`` (styling stripped)."""
    return text_obj.plain


@pytest.mark.parametrize(
    "code, expected",
    [("zh", "\u8bfb\u53d6 1 \u884c"), ("en", "read 1 line")],
)
def test_read_one_line_is_singular(code: str, expected: str) -> None:
    with use_locale(code):
        assert _summary_read(_READ_1) == expected


@pytest.mark.parametrize(
    "code, expected",
    [("zh", "\u8bfb\u53d6 3 \u884c"), ("en", "read 3 lines")],
)
def test_read_many_lines_is_plural(code: str, expected: str) -> None:
    with use_locale(code):
        assert _summary_read(_READ_3) == expected


@pytest.mark.parametrize(
    "code, expected",
    [
        ("zh", "\u627e\u5230 1 \u5904\u5339\u914d\uff0c\u5171 1 \u4e2a\u6587\u4ef6"),
        ("en", "found 1 match across 1 file"),
    ],
)
def test_grep_matches_files_singular(code: str, expected: str) -> None:
    text = "Found 1 total occurrence across 1 files"
    with use_locale(code):
        assert _summary_search(text) == expected


@pytest.mark.parametrize(
    "code, expected",
    [
        ("zh", "\u627e\u5230 7 \u5904\u5339\u914d\uff0c\u5171 3 \u4e2a\u6587\u4ef6"),
        ("en", "found 7 matches across 3 files"),
    ],
)
def test_grep_matches_files_plural(code: str, expected: str) -> None:
    text = "Found 7 total occurrences across 3 files"
    with use_locale(code):
        assert _summary_search(text) == expected


@pytest.mark.parametrize(
    "code, expected",
    [("zh", "\u65b0\u5efa 5 \u884c"), ("en", "created 5 lines")],
)
def test_write_created(code: str, expected: str) -> None:
    # A whole-file write now flows through Edit: an empty ``old`` + all-new
    # ``new`` reads as "created N lines".
    event = SimpleNamespace(
        file_changes=[SimpleNamespace(old="", new="a\nb\nc\nd\ne\n")],
    )
    with use_locale(code):
        assert _summary_edit(event, "") == expected


@pytest.mark.parametrize(
    "code, expected",
    [("zh", "\u66f4\u65b0 +3 -2 \u884c"), ("en", "updated +3 -2 lines")],
)
def test_edit_added_removed(code: str, expected: str) -> None:
    event = SimpleNamespace(
        file_changes=[SimpleNamespace(old="a\nb\n", new="A\nB\nc\n")],
    )
    with use_locale(code):
        assert _summary_edit(event, "") == expected


@pytest.mark.parametrize(
    "code, expected",
    [("zh", "\u66ff\u6362 1 \u5904"), ("en", "replaced 1 occurrence")],
)
def test_edit_replaced_singular(code: str, expected: str) -> None:
    with use_locale(code):
        assert _summary_edit(SimpleNamespace(file_changes=[]), "All 1 occurrence replaced") == expected


@pytest.mark.parametrize(
    "code, expected",
    [
        ("zh", "\u2026 +1 \u884c\u5df2\u6298\u53e0"),
        ("en", "\u2026 +1 line folded"),
    ],
)
def test_fold_hidden_lines_singular(code: str, expected: str) -> None:
    ev = SimpleNamespace(full_ref=None, hidden_lines=1)
    with use_locale(code):
        assert fold_note_str(ev) == expected


@pytest.mark.parametrize(
    "code, expected_tail",
    [
        ("zh", "\u641c\u7d22 2 \u4e2a\u6a21\u5f0f\uff0c\u8bfb\u53d6 1 \u4e2a\u6587\u4ef6"),
        ("en", "searched 2 patterns, read 1 file"),
    ],
)
def test_tool_group_summary(code: str, expected_tail: str) -> None:
    items = [("Search", None), ("Search", None), ("Read", "/a.py")]
    with use_locale(code):
        got = _plain(tool_group_summary_text(items, active=False))
    assert got.endswith(expected_tail)


@pytest.mark.parametrize(
    "code, expected_tail",
    [
        ("zh", "\u5bf9\u8bdd\u5df2\u538b\u7f29 (\u4fdd\u7559 1 \u6761\u6d88\u606f)"),
        ("en", "Conversation compacted (kept 1 message)"),
    ],
)
def test_conversation_compacted(code: str, expected_tail: str) -> None:
    ev = SimpleNamespace(message_count=1)
    with use_locale(code):
        got = _plain(conversation_compacted_text(ev))
    assert got.endswith(expected_tail)


@pytest.mark.parametrize(
    "code, expected_tail",
    [("zh", "\u2026 +2 \u884c"), ("en", "\u2026 +2 lines")],
)
def test_compaction_summary_fold_tail(code: str, expected_tail: str) -> None:
    summary = "\n".join(f"line {i}" for i in range(14))
    with use_locale(code):
        got = _plain(compaction_summary_text(summary, max_lines=12))
    assert got.endswith(expected_tail)


def test_locale_switch_flips_wording() -> None:
    # The same call renders differently per locale — the seam actually works.
    with use_locale("zh"):
        zh = _summary_read(_READ_3)
    with use_locale("en"):
        en = _summary_read(_READ_3)
    assert zh != en
    assert zh == "\u8bfb\u53d6 3 \u884c"
    assert en == "read 3 lines"
