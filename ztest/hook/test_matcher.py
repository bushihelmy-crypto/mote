#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for HookManager._matches (port of CC matchesPattern)."""
from __future__ import annotations

from mote.common.hook.manager import HookManager

m = HookManager._matches


def test_empty_and_star_match_all():
    assert m(None, "Bash") is True
    assert m("", "Bash") is True
    assert m("*", "Bash") is True


def test_no_query_always_matches():
    # Events without a match field pass query=None and always fire.
    assert m("Bash", None) is True
    assert m("anything", None) is True


def test_pipe_exact_list():
    assert m("Bash|Write", "Bash") is True
    assert m("Bash|Write", "Write") is True
    assert m("Bash|Write", "Read") is False


def test_regex_match():
    assert m("Edit.*", "EditFile") is True
    assert m("^Bash$", "Bash") is True
    assert m("^Bash$", "BashTool") is False


def test_malformed_regex_falls_back_to_exact():
    # An unbalanced bracket is an invalid regex -> exact compare.
    assert m("[", "[") is True
    assert m("[", "x") is False
