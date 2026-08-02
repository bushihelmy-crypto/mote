#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.vcs.collector`` — read-only git snapshot.

Covers: non-repo / empty cwd -> None; branch read filesystem-first from
.git/HEAD; detached HEAD; staged / unstaged / untracked counting; recent-commit
list; the short-TTL cache; and that any failure degrades to None rather than
raising. Uses a real throwaway git repo in tmp_path.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from mote.runtime.context.turn import GitContextSource, TurnContextBus
from mote.runtime.vcs import collector
from mote.runtime.vcs.collector import GitState, _parse_status, _read_branch, collect_git_state

# Async tests are decorated individually so the pure-function unit tests at the
# bottom don't inherit a spurious asyncio mark.
aio = pytest.mark.asyncio


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(path) -> str:
    repo = str(path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    # Force a deterministic default branch name regardless of git version.
    _git(repo, "checkout", "-q", "-b", "main")
    return repo


@pytest.fixture(autouse=True)
def _clear_cache():
    collector._cache.clear()
    yield
    collector._cache.clear()


@aio
async def test_non_repo_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "find_git_dir", lambda _cwd: None)
    assert await collect_git_state(str(tmp_path)) is None


@aio
async def test_empty_cwd_returns_none():
    assert await collect_git_state("") is None


@aio
async def test_clean_repo_branch_and_status(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    state = await collect_git_state(repo)
    assert state is not None
    assert state.repo_root == os.path.abspath(repo)
    assert state.branch == "main"
    assert state.detached_sha is None
    assert state.clean is True
    assert state.staged == 0 and state.unstaged == 0 and state.untracked == 0
    assert len(state.recent_commits) == 1
    assert "first commit" in state.recent_commits[0]


@aio
async def test_dirty_counts(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")

    # staged modification
    (tmp_path / "a.txt").write_text("changed\n")
    _git(repo, "add", "a.txt")
    # unstaged modification on top of the staged one
    (tmp_path / "a.txt").write_text("changed again\n")
    # untracked file
    (tmp_path / "b.txt").write_text("new\n")

    state = await collect_git_state(repo)
    assert state is not None
    assert state.clean is False
    assert state.staged == 1
    assert state.unstaged == 1
    assert state.untracked == 1


@aio
async def test_git_source_does_not_block_persistent_turn_context(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first commit")
    source = GitContextSource(get_cwd=lambda: repo)
    bus = TurnContextBus([source])

    reminder = await asyncio.wait_for(bus.collect_to_context(cwd=repo), timeout=1.0)

    assert "<system-reminder>" in reminder
    assert "main" in reminder


@aio
async def test_recent_commits_capped(tmp_path):
    repo = _init_repo(tmp_path)
    for i in range(8):
        (tmp_path / "f.txt").write_text(f"v{i}\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"commit {i}")

    state = await collect_git_state(repo)
    assert state is not None
    # Capped at _RECENT_COMMITS (5), newest first.
    assert len(state.recent_commits) == collector._RECENT_COMMITS
    assert "commit 7" in state.recent_commits[0]


@aio
async def test_detached_head(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c1")
    (tmp_path / "a.txt").write_text("y\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c2")
    # Detach onto the first commit.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", head)

    state = await collect_git_state(repo)
    assert state is not None
    assert state.branch is None
    assert state.detached_sha is not None
    assert head.startswith(state.detached_sha)


@aio
async def test_cache_reused_within_ttl(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c1")

    first = await collect_git_state(repo)
    assert first is not None

    # A second call within the TTL must return the SAME cached object without
    # re-shelling out to git.
    calls = {"n": 0}
    real_git = collector._git

    async def _counting_git(cwd, args):
        calls["n"] += 1
        return await real_git(cwd, args)

    monkeypatch.setattr(collector, "_git", _counting_git)
    second = await collect_git_state(repo)
    assert second is first
    assert calls["n"] == 0


@aio
async def test_collect_never_raises_on_git_failure(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)

    async def _boom(*args, **kwargs):
        raise RuntimeError("git exploded")

    # The fixed argv runner blowing up is exactly what _git is designed to
    # swallow (best-effort on the prompt-build path). collect must degrade to
    # zero counts / no commits rather than raise; branch is still read
    # filesystem-first from HEAD (which exists once initialised).
    monkeypatch.setattr(collector, "run_fixed_argv", _boom)
    state = await collect_git_state(repo)
    assert state is not None
    assert state.staged == 0 and state.unstaged == 0 and state.untracked == 0
    assert state.recent_commits == []


# --- pure-function unit tests (no subprocess) ---------------------------------


def test_parse_status_counts():
    porcelain = "\n".join(
        [
            "M  staged.py",  # staged modification
            " M unstaged.py",  # unstaged modification
            "MM both.py",  # staged + unstaged
            "?? new.py",  # untracked
            "A  added.py",  # staged add
        ]
    )
    staged, unstaged, untracked = _parse_status(porcelain)
    assert staged == 3  # staged.py, both.py, added.py
    assert unstaged == 2  # unstaged.py, both.py
    assert untracked == 1  # new.py


def test_parse_status_empty():
    assert _parse_status("") == (0, 0, 0)


def test_read_branch_from_ref(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/feature/x\n")
    branch, sha = _read_branch(str(git_dir))
    assert branch == "feature/x"
    assert sha is None


def test_read_branch_detached(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("a" * 40 + "\n")
    branch, sha = _read_branch(str(git_dir))
    assert branch is None
    assert sha == "a" * 8


def test_git_state_clean_property():
    assert GitState(repo_root="/r").clean is True
    assert GitState(repo_root="/r", staged=1).clean is False
    assert GitState(repo_root="/r", untracked=2).clean is False
