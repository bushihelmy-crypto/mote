#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.runtime.terminal_ansi`."""
from __future__ import annotations

from mote.runtime.terminal_ansi import strip_ansi


class TestStripAnsi:
    def test_colour_and_bold_stripped(self):
        colored = "\x1b[31m\x1b[1mred and bold\x1b[0m"
        assert strip_ansi(colored) == "red and bold"

    def test_simple_colour(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_no_escapes_unchanged(self):
        assert strip_ansi("no escapes here") == "no escapes here"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_cursor_move_stripped(self):
        # CSI cursor-up (``ESC[3A``) and clear-line (``ESC[2K``).
        assert strip_ansi("\x1b[3A\x1b[2Kline") == "line"

    def test_question_mark_param_stripped(self):
        # Private-mode set/reset, e.g. hide cursor ``ESC[?25l``.
        assert strip_ansi("\x1b[?25lhidden\x1b[?25h") == "hidden"

    def test_multiline_preserved(self):
        assert strip_ansi("\x1b[32ma\x1b[0m\nb") == "a\nb"
