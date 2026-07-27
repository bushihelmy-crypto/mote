#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for whole-tree checkpoints (the user-facing ``/rewind`` layer).

Covers :class:`CheckpointStore` (capture/restore round-trips through a dedicated
bare git repo, over a real working tree), :func:`list_checkpoints` (forward log
scan, chronological order, monotonic index across resume), and
:class:`CheckpointSubscriber` (one checkpoint per user turn, index seeded across
resume, inert without a working dir).

The delicate part is ``restore``: it must revert edits, remove agent-created
untracked files, re-create deleted files, and yet leave gitignored build
artifacts alone (no ``-x``). Each of those is asserted explicitly.

Tests needing the ``git`` binary skip when it is unavailable.
"""
from __future__ import annotations

import shutil

import pytest

from mote.contracts.events.types import TurnEndEvent
from mote.contracts.fileops import RewindFailedError
from mote.runtime.events import UserPromptSubmitEvent
from mote.runtime.fileops import FileOperations, WorktreeCheckpointStore
from mote.runtime.session.checkpoint import list_checkpoints
from mote.runtime.session.codec import iter_file_operations_events
from mote.runtime.session.events import CHECKPOINT, CheckpointEvent, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.subscribers import CheckpointSubscriber

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")


def _log(tmp_path, session_id: str) -> SessionLog:
    log = SessionLog(session_id, base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id=session_id))
    return log


def _store(tmp_path):
    """A CheckpointStore over a fresh work-tree + a sibling dedicated git db."""
    work = tmp_path / "work"
    work.mkdir()
    git_dir = tmp_path / "session" / "git"
    return WorktreeCheckpointStore(git_dir, work), work


def _subscriber(log, get_working_dir, *, enabled=True):
    operations = FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=get_working_dir,
        flush_pending=log.writer.flush_inline,
        lock_root=log.path.parent.parent / "locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )
    return CheckpointSubscriber(
        log,
        get_working_dir,
        operations.capture_worktree_checkpoint,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# CheckpointStore.capture / restore
# ---------------------------------------------------------------------------


@git_required
def test_capture_returns_commit_and_pins_repo(tmp_path):
    store, work = _store(tmp_path)
    (work / "a.txt").write_text("v1\n")
    commit = store.capture()
    assert commit and len(commit) == 40  # git commit sha1
    # A dedicated bare repo materialized (never the user's own .git).
    assert (tmp_path / "session" / "git" / "HEAD").exists()


@git_required
def test_edit_then_restore_reverts_content(tmp_path):
    store, work = _store(tmp_path)
    f = work / "a.txt"
    f.write_text("v1\n")
    commit = store.capture()

    f.write_text("v2 mutated\n")
    store.restore(commit)
    assert f.read_text() == "v1\n"


@git_required
def test_restore_removes_agent_created_untracked_file(tmp_path):
    """A Bash-created file (untracked at checkpoint) is removed by restore."""
    store, work = _store(tmp_path)
    (work / "a.txt").write_text("v1\n")
    commit = store.capture()

    created = work / "scratch.log"
    created.write_text("agent output\n")
    store.restore(commit)
    assert not created.exists()
    assert (work / "a.txt").read_text() == "v1\n"


@git_required
def test_restore_recreates_deleted_file(tmp_path):
    store, work = _store(tmp_path)
    f = work / "a.txt"
    f.write_text("keep me\n")
    commit = store.capture()

    f.unlink()
    store.restore(commit)
    assert f.exists() and f.read_text() == "keep me\n"


@git_required
def test_gitignored_artifact_survives_restore(tmp_path):
    """restore uses no ``-x`` — gitignored build artifacts must NOT be cleaned."""
    store, work = _store(tmp_path)
    (work / ".gitignore").write_text("build/\n")
    (work / "a.txt").write_text("src\n")
    commit = store.capture()

    build = work / "build"
    build.mkdir()
    artifact = build / "out.o"
    artifact.write_text("binary\n")

    store.restore(commit)
    # The gitignored artifact survives (clean without -x respects .gitignore).
    assert artifact.exists()


@git_required
def test_capture_chains_on_parent(tmp_path):
    store, work = _store(tmp_path)
    (work / "a.txt").write_text("one\n")
    c1 = store.capture()
    (work / "a.txt").write_text("two\n")
    c2 = store.capture(parent=c1)
    assert c1 != c2
    # Restoring the first checkpoint still recovers the original content.
    store.restore(c1)
    assert (work / "a.txt").read_text() == "one\n"


def test_capture_no_work_dir_fails_explicitly(tmp_path):
    store = WorktreeCheckpointStore(tmp_path / "git", tmp_path / "does-not-exist")
    with pytest.raises(RewindFailedError):
        store.capture()


def test_restore_without_repo_fails_explicitly(tmp_path):
    store, _ = _store(tmp_path)
    with pytest.raises(RewindFailedError):
        store.restore("0" * 40)


# ---------------------------------------------------------------------------
# list_checkpoints (read side)
# ---------------------------------------------------------------------------


def test_list_checkpoints_empty(tmp_path):
    log = _log(tmp_path, "cp_empty")
    assert list_checkpoints(log) == []


def test_list_checkpoints_in_order_with_index(tmp_path):
    log = _log(tmp_path, "cp_order")
    log.commit_offline(CheckpointEvent(commit="a" * 40, prompt_index=0, prompt_preview="first"))
    log.commit_offline(CheckpointEvent(commit="b" * 40, prompt_index=1, prompt_preview="second"))
    log.commit_offline(CheckpointEvent(commit="c" * 40, prompt_index=2, prompt_preview="third"))

    entries = list_checkpoints(log)
    assert [e.index for e in entries] == [0, 1, 2]
    assert [e.commit for e in entries] == ["a" * 40, "b" * 40, "c" * 40]
    assert [e.prompt_preview for e in entries] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# CheckpointSubscriber (one checkpoint per user turn)
# ---------------------------------------------------------------------------


@git_required
@pytest.mark.asyncio
async def test_subscriber_captures_one_checkpoint_per_prompt(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("v1\n")
    log = _log(tmp_path, "cp_sub")
    sub = _subscriber(log, lambda: str(work))

    await sub.handle(UserPromptSubmitEvent(prompt="do the thing"))
    (work / "a.txt").write_text("v2\n")
    await sub.handle(UserPromptSubmitEvent(prompt="do another"))

    entries = list_checkpoints(log)
    assert len(entries) == 2
    assert [e.prompt_index for e in entries] == [0, 1]
    assert entries[0].prompt_preview == "do the thing"
    # Restoring the first checkpoint recovers the pre-turn content.
    store = WorktreeCheckpointStore(log.path.parent / "git", work)
    store.restore(entries[0].commit)
    assert (work / "a.txt").read_text() == "v1\n"


@git_required
@pytest.mark.asyncio
async def test_subscriber_prompt_index_monotonic_across_resume(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("x\n")
    log = _log(tmp_path, "cp_resume")

    sub1 = _subscriber(log, lambda: str(work))
    await sub1.handle(UserPromptSubmitEvent(prompt="p0"))
    await sub1.handle(UserPromptSubmitEvent(prompt="p1"))

    # Fresh subscriber over the same log (a resumed process) continues the count.
    sub2 = _subscriber(log, lambda: str(work))
    await sub2.handle(UserPromptSubmitEvent(prompt="p2"))

    entries = list_checkpoints(log)
    assert [e.prompt_index for e in entries] == [0, 1, 2]


@pytest.mark.asyncio
async def test_subscriber_no_working_dir_is_noop(tmp_path):
    log = _log(tmp_path, "cp_nowd")
    sub = _subscriber(log, lambda: "")
    await sub.handle(UserPromptSubmitEvent(prompt="ignored"))
    assert list_checkpoints(log) == []


@pytest.mark.asyncio
async def test_subscriber_disabled_is_noop(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    log = _log(tmp_path, "cp_off")
    sub = _subscriber(log, lambda: str(work), enabled=False)
    await sub.handle(UserPromptSubmitEvent(prompt="ignored"))
    assert list_checkpoints(log) == []


@pytest.mark.asyncio
async def test_subscriber_ignores_non_prompt_events(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    log = _log(tmp_path, "cp_other")
    sub = _subscriber(log, lambda: str(work))
    # A non-UserPromptSubmit / non-TurnEnd event must not capture anything.
    await sub.handle(object())
    assert list_checkpoints(log) == []


# ---------------------------------------------------------------------------
# after-snapshot: turn-end capture + external-modification detection
# ---------------------------------------------------------------------------


@git_required
def test_diff_tree_reports_changed_paths(tmp_path):
    store, work = _store(tmp_path)
    (work / "a.txt").write_text("one\n")
    (work / "b.txt").write_text("keep\n")
    c1 = store.capture()
    (work / "a.txt").write_text("two\n")  # modified
    (work / "c.txt").write_text("new\n")  # created
    c2 = store.capture(parent=c1)
    changed = set(store.diff_tree(c1, c2))
    assert changed == {"a.txt", "c.txt"}  # b.txt unchanged, absent


@git_required
def test_diff_tree_empty_when_identical(tmp_path):
    store, work = _store(tmp_path)
    (work / "a.txt").write_text("same\n")
    c1 = store.capture()
    c2 = store.capture(parent=c1)  # nothing changed on disk
    assert store.diff_tree(c1, c2) == []


def test_diff_tree_missing_commit_is_empty(tmp_path):
    store, _ = _store(tmp_path)
    # No repo / empty args → graceful [] (never raises).
    assert store.diff_tree("", "x") == []
    assert store.diff_tree("a" * 40, "") == []


@git_required
@pytest.mark.asyncio
async def test_turn_end_records_after_commit_folded_onto_entry(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("before\n")
    log = _log(tmp_path, "cp_after")
    sub = _subscriber(log, lambda: str(work))

    await sub.handle(UserPromptSubmitEvent(prompt="edit it"))
    (work / "a.txt").write_text("agent-left\n")  # what the agent produced
    await sub.handle(TurnEndEvent())

    entries = list_checkpoints(log)
    # One user turn → one entry (the after-event is folded, not a second entry).
    assert len(entries) == 1
    assert entries[0].after_commit and entries[0].after_commit != entries[0].commit


@git_required
@pytest.mark.asyncio
async def test_turn_end_without_prior_prompt_is_noop(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("x\n")
    log = _log(tmp_path, "cp_after_noop")
    sub = _subscriber(log, lambda: str(work))
    # A TurnEndEvent with no in-flight prompt captures nothing.
    await sub.handle(TurnEndEvent())
    assert list_checkpoints(log) == []


@git_required
@pytest.mark.asyncio
async def test_after_commit_captures_external_change_vs_live_tree(tmp_path):
    """The end-to-end signal /rewind uses: diff after-image vs the live tree."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("before\n")
    log = _log(tmp_path, "cp_ext")
    sub = _subscriber(log, lambda: str(work))

    await sub.handle(UserPromptSubmitEvent(prompt="edit it"))
    (work / "a.txt").write_text("agent-left\n")
    await sub.handle(TurnEndEvent())

    entry = list_checkpoints(log)[0]
    # An external process changes a file after the agent finished.
    (work / "a.txt").write_text("someone-else-edited\n")
    store = WorktreeCheckpointStore(log.path.parent / "git", work)
    live = store.capture(parent=entry.after_commit)
    external = store.diff_tree(entry.after_commit, live)
    assert external == ["a.txt"]
