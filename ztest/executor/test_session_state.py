#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the shared session env-state diff (kernel / terminal)."""

from mote.runtime.tools.dependency._session_state import diff_env_state

_NOISE = frozenset({"PWD", "SHLVL", "_"})


def test_none_probe_returns_none():
    assert diff_env_state(None, {"A": "1"}, _NOISE) is None


def test_added_and_changed_keys_go_to_diff():
    baseline = {"A": "1", "B": "2"}
    probed = ("/work", {"A": "1", "B": "9", "C": "3"})
    cwd, diff, unset = diff_env_state(probed, baseline, _NOISE)
    assert cwd == "/work"
    assert diff == {"B": "9", "C": "3"}  # B changed, C added; A unchanged
    assert unset == []


def test_removed_keys_go_to_unset():
    baseline = {"A": "1", "B": "2"}
    probed = ("/work", {"A": "1"})
    _, diff, unset = diff_env_state(probed, baseline, _NOISE)
    assert diff == {}
    assert unset == ["B"]


def test_noise_keys_filtered_from_both_sides():
    baseline = {"PWD": "/old", "SHLVL": "1", "KEEP": "x"}
    probed = ("/new", {"PWD": "/new", "KEEP": "x", "_": "junk"})
    cwd, diff, unset = diff_env_state(probed, baseline, _NOISE)
    assert cwd == "/new"
    assert diff == {}  # PWD/_ are noise; KEEP unchanged
    assert unset == []  # SHLVL gone but it's noise → not reported
