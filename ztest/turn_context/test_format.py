#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.context.turn_context.format.wrap_system_reminder."""
from __future__ import annotations

from metagpt.context.turn_context import wrap_system_reminder


class TestWrapSystemReminder:
    def test_empty_iterable_returns_empty(self):
        assert wrap_system_reminder([]) == ""

    def test_all_blank_returns_empty(self):
        assert wrap_system_reminder(["", "   ", "\n"]) == ""

    def test_none_entries_dropped(self):
        out = wrap_system_reminder([None, "a", None])
        assert out == "<system-reminder>\na\n</system-reminder>"

    def test_single_block_wrapped(self):
        out = wrap_system_reminder(["hello"])
        assert out == "<system-reminder>\nhello\n</system-reminder>"

    def test_multiple_blocks_joined_with_blank_line(self):
        out = wrap_system_reminder(["a", "b"])
        assert out == "<system-reminder>\na\n\nb\n</system-reminder>"

    def test_blocks_are_stripped(self):
        out = wrap_system_reminder(["  a  ", "\nb\n"])
        assert out == "<system-reminder>\na\n\nb\n</system-reminder>"
