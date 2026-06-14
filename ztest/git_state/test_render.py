#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.common.git_state.render`` — GitState -> env lines."""
from __future__ import annotations

from metagpt.common.git_state.collector import GitState
from metagpt.common.git_state.render import render_git_section


def test_render_none_is_empty():
    assert render_git_section(None) == ""


def test_render_clean_branch():
    state = GitState(repo_root="/r", branch="main")
    out = render_git_section(state)
    assert " - Git branch: main" in out
    assert " - Git status: clean" in out
    assert "Recent commits" not in out


def test_render_dirty_counts():
    state = GitState(repo_root="/r", branch="dev", staged=2, unstaged=1, untracked=3)
    out = render_git_section(state)
    assert " - Git status: dirty (2 staged, 1 unstaged, 3 untracked)" in out


def test_render_partial_dirty():
    state = GitState(repo_root="/r", branch="dev", untracked=1)
    out = render_git_section(state)
    assert " - Git status: dirty (1 untracked)" in out
    assert "staged" not in out


def test_render_detached():
    state = GitState(repo_root="/r", branch=None, detached_sha="abc12345")
    out = render_git_section(state)
    assert " - Git branch: detached @ abc12345" in out


def test_render_unknown_head():
    state = GitState(repo_root="/r", branch=None, detached_sha=None)
    out = render_git_section(state)
    assert " - Git branch: unknown" in out


def test_render_recent_commits():
    state = GitState(
        repo_root="/r",
        branch="main",
        recent_commits=["abc123 first", "def456 second"],
    )
    out = render_git_section(state)
    assert " - Recent commits:" in out
    assert "     abc123 first" in out
    assert "     def456 second" in out
