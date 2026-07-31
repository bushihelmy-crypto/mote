#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.runtime.context.markers`."""
from __future__ import annotations

from mote.contracts.tool.output_markers import PERSISTED_OUTPUT_CLOSE, PERSISTED_OUTPUT_OPEN
from mote.runtime.context.markers import (
    SYSTEM_REMINDER_CLOSE,
    SYSTEM_REMINDER_OPEN,
    is_system_reminder,
    strip_system_reminder,
    system_reminder,
    wrap_system_reminder,
)


class TestTagLiterals:
    def test_system_reminder_literals(self):
        assert SYSTEM_REMINDER_OPEN == "<system-reminder>"
        assert SYSTEM_REMINDER_CLOSE == "</system-reminder>"

    def test_persisted_output_literals(self):
        assert PERSISTED_OUTPUT_OPEN == "<persisted-output>"
        assert PERSISTED_OUTPUT_CLOSE == "</persisted-output>"


class TestWrapSystemReminder:
    def test_empty_iterable_returns_empty(self):
        assert wrap_system_reminder([]) == ""

    def test_all_blank_returns_empty(self):
        assert wrap_system_reminder(["", "   ", "\n"]) == ""

    def test_none_entries_dropped(self):
        assert wrap_system_reminder([None, "a", None]) == "<system-reminder>\na\n</system-reminder>"

    def test_single_block_wrapped(self):
        assert wrap_system_reminder(["hello"]) == "<system-reminder>\nhello\n</system-reminder>"

    def test_multiple_blocks_joined_with_blank_line(self):
        assert wrap_system_reminder(["a", "b"]) == "<system-reminder>\na\n\nb\n</system-reminder>"

    def test_blocks_are_stripped(self):
        assert wrap_system_reminder(["  a  ", "\nb\n"]) == "<system-reminder>\na\n\nb\n</system-reminder>"


class TestIsSystemReminder:
    def test_exact_envelope_true(self):
        assert is_system_reminder("<system-reminder>\nfoo\n</system-reminder>")

    def test_surrounding_whitespace_tolerated(self):
        assert is_system_reminder("  <system-reminder>x</system-reminder>  ")

    def test_prose_mention_not_matched(self):
        # A human prompt that merely references the tag in the middle is not an envelope.
        assert not is_system_reminder("please look at <system-reminder> in the code")

    def test_open_only_not_matched(self):
        assert not is_system_reminder("<system-reminder>unclosed")

    def test_close_only_not_matched(self):
        assert not is_system_reminder("no open</system-reminder>")


class TestStripSystemReminder:
    def test_roundtrip_with_wrap(self):
        wrapped = wrap_system_reminder(["alpha", "beta"])
        assert strip_system_reminder(wrapped) == "alpha\n\nbeta"

    def test_strips_both_tags(self):
        assert strip_system_reminder("<system-reminder>body</system-reminder>") == "body"

    def test_no_tags_returns_stripped_content(self):
        assert strip_system_reminder("  plain text  ") == "plain text"


class TestSystemReminder:
    def test_wraps_inline(self):
        assert system_reminder("Warning: empty") == "<system-reminder>Warning: empty</system-reminder>"
