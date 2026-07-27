#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.context.prompt`` — the compaction prompt builders.

Covers the two summarization prompts (full vs partial/up_to, plus custom-
instruction appending and the no-tools preamble/trailer), ``format_compact_summary``
(strip ``<analysis>``, unwrap ``<summary>`` → ``Summary:`` header, collapse blanks,
handle tag-less input) and ``get_compact_user_summary_message`` (the continued-
session preface and its optional transcript / recent-preserved / suppress
branches).
"""
from __future__ import annotations

from mote.runtime.context import prompt


def test_compact_prompt_has_preamble_body_trailer():
    p = prompt.get_compact_prompt()
    assert p.startswith(prompt.NO_TOOLS_PREAMBLE.rstrip()[:20])
    assert "Primary Request and Intent" in p
    assert "<analysis>" in p
    assert "<summary>" in p
    assert p.endswith(prompt.NO_TOOLS_TRAILER)


def test_compact_prompt_appends_custom_instructions():
    p = prompt.get_compact_prompt("Focus on the database layer")
    assert "Additional Instructions:" in p
    assert "Focus on the database layer" in p


def test_compact_prompt_blank_custom_is_ignored():
    assert "Additional Instructions" not in prompt.get_compact_prompt("   ")
    assert "Additional Instructions" not in prompt.get_compact_prompt(None)


def test_partial_prompt_mentions_following_messages():
    p = prompt.get_partial_compact_prompt()
    # 'up_to' variant tells the model newer (unseen) messages follow the summary.
    assert "newer messages" in p
    assert "Context for Continuing Work" in p
    assert p.endswith(prompt.NO_TOOLS_TRAILER)


def test_partial_and_full_prompts_differ():
    assert prompt.get_partial_compact_prompt() != prompt.get_compact_prompt()


def test_partial_prompt_appends_custom_instructions():
    p = prompt.get_partial_compact_prompt("keep the API contract")
    assert "Additional Instructions:" in p
    assert "keep the API contract" in p


# ---------------------------------------------------------------------------
# format_compact_summary
# ---------------------------------------------------------------------------


def test_format_strips_analysis_and_unwraps_summary():
    raw = "<analysis>scratch thoughts</analysis>\n\n<summary>the real summary</summary>"
    out = prompt.format_compact_summary(raw)
    assert "scratch thoughts" not in out
    assert "<analysis>" not in out
    assert "<summary>" not in out
    assert out.startswith("Summary:")
    assert "the real summary" in out


def test_format_collapses_blank_runs():
    raw = "<summary>line1\n\n\n\nline2</summary>"
    out = prompt.format_compact_summary(raw)
    assert "\n\n\n" not in out


def test_format_without_tags_returns_cleaned_text():
    out = prompt.format_compact_summary("just a plain summary, no tags")
    assert out == "just a plain summary, no tags"


# ---------------------------------------------------------------------------
# get_compact_user_summary_message
# ---------------------------------------------------------------------------


def test_user_summary_message_has_continued_preface():
    msg = prompt.get_compact_user_summary_message("<summary>S</summary>")
    assert "continued from a previous conversation" in msg
    assert "Summary:" in msg
    assert "S" in msg


def test_user_summary_message_transcript_branch():
    with_path = prompt.get_compact_user_summary_message("<summary>S</summary>", transcript_path="/tmp/t.json")
    without = prompt.get_compact_user_summary_message("<summary>S</summary>")
    assert "/tmp/t.json" in with_path
    assert "/tmp/t.json" not in without


def test_user_summary_message_recent_preserved_branch():
    msg = prompt.get_compact_user_summary_message("<summary>S</summary>", recent_messages_preserved=True)
    assert "Recent messages are preserved verbatim." in msg


def test_user_summary_message_suppress_branch():
    msg = prompt.get_compact_user_summary_message("<summary>S</summary>", suppress_follow_up_questions=True)
    assert "without asking" in msg
    assert "Resume directly" in msg


def test_user_summary_message_default_omits_optional_clauses():
    msg = prompt.get_compact_user_summary_message("<summary>S</summary>")
    assert "Recent messages are preserved" not in msg
    assert "without asking" not in msg
