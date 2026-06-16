#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the FileWatcher core (Phase 3).

Drives :meth:`FileWatcher.poll` directly (no async loop) to verify create /
modify / delete detection, baseline priming, and ignore-pattern pruning.
"""
from __future__ import annotations

import asyncio
import os

from metagpt.environment.watching.events import CREATED, DELETED, MODIFIED, FileChangeEvent
from metagpt.environment.watching.watcher import FileWatcher


def _collect():
    """Return (on_change coroutine, captured-list) recording every event."""
    captured: list[FileChangeEvent] = []

    async def on_change(event: FileChangeEvent) -> None:
        captured.append(event)

    return on_change, captured


def _watcher(tmp_path, **kw):
    on_change, captured = _collect()
    return FileWatcher([str(tmp_path)], on_change, **kw), captured


def test_unprimed_first_poll_emits_created_for_existing(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    w, captured = _watcher(tmp_path)
    events = asyncio.run(w.poll())
    assert {e.change_type for e in events} == {CREATED}
    assert [e.path for e in captured] == [str(tmp_path / "a.txt")]


def test_prime_suppresses_initial_burst(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    w, captured = _watcher(tmp_path)
    w.prime()
    events = asyncio.run(w.poll())
    assert events == []
    assert captured == []


def test_detects_new_file_after_prime(tmp_path):
    w, captured = _watcher(tmp_path)
    w.prime()
    (tmp_path / "new.txt").write_text("hello")
    events = asyncio.run(w.poll())
    assert len(events) == 1
    assert events[0].change_type == CREATED
    assert events[0].path == str(tmp_path / "new.txt")
    assert events[0].size == len("hello")


def test_detects_modification(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("v1")
    w, _ = _watcher(tmp_path)
    w.prime()
    # Bump mtime deterministically (size also changes).
    target.write_text("v2-longer")
    events = asyncio.run(w.poll())
    assert len(events) == 1
    assert events[0].change_type == MODIFIED


def test_detects_same_size_modification_via_mtime(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa")
    w, _ = _watcher(tmp_path)
    w.prime()
    # Same size, different mtime -> still a modification (mtime_ns in signature).
    os.utime(target, ns=(10**18, 10**18))
    events = asyncio.run(w.poll())
    assert [e.change_type for e in events] == [MODIFIED]


def test_detects_deletion(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("bye")
    w, _ = _watcher(tmp_path)
    w.prime()
    os.remove(target)
    events = asyncio.run(w.poll())
    assert len(events) == 1
    assert events[0].change_type == DELETED
    assert events[0].path == str(target)
    assert events[0].size == 0


def test_no_changes_emits_nothing(tmp_path):
    (tmp_path / "f.txt").write_text("stable")
    w, _ = _watcher(tmp_path)
    w.prime()
    assert asyncio.run(w.poll()) == []


def test_ignore_prunes_matching_files(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.pyc").write_text("s")
    w, _ = _watcher(tmp_path, ignore=["*.pyc"])
    events = asyncio.run(w.poll())
    paths = {e.path for e in events}
    assert str(tmp_path / "keep.txt") in paths
    assert str(tmp_path / "skip.pyc") not in paths


def test_ignore_prunes_directories(tmp_path):
    sub = tmp_path / ".git"
    sub.mkdir()
    (sub / "config").write_text("ignored")
    (tmp_path / "real.txt").write_text("seen")
    w, _ = _watcher(tmp_path, ignore=[".git"])
    events = asyncio.run(w.poll())
    paths = {e.path for e in events}
    assert str(tmp_path / "real.txt") in paths
    assert all(".git" not in p for p in paths)


def test_watches_single_file_root(tmp_path):
    target = tmp_path / "only.txt"
    target.write_text("a")
    on_change, captured = _collect()
    w = FileWatcher([str(target)], on_change)
    w.prime()
    target.write_text("bb")
    events = asyncio.run(w.poll())
    assert [e.change_type for e in events] == [MODIFIED]


def test_multiple_changes_in_one_poll(tmp_path):
    keep = tmp_path / "keep.txt"
    gone = tmp_path / "gone.txt"
    keep.write_text("1")
    gone.write_text("2")
    w, _ = _watcher(tmp_path)
    w.prime()
    keep.write_text("1-changed")
    os.remove(gone)
    (tmp_path / "added.txt").write_text("3")
    events = asyncio.run(w.poll())
    by_type = {e.change_type for e in events}
    assert by_type == {CREATED, MODIFIED, DELETED}


def test_self_write_suppressed(tmp_path):
    """A file noted as a self-write is not reported on the next poll."""
    target = tmp_path / "f.txt"
    target.write_text("v1")
    w, _ = _watcher(tmp_path)
    w.prime()
    # Simulate the agent's own tool writing the file, then noting it.
    target.write_text("v2-by-agent")
    w.note_self_write(str(target))
    assert asyncio.run(w.poll()) == []


def test_self_write_note_consumed_after_one_poll(tmp_path):
    """The note is one-shot: a later genuine change is reported normally."""
    target = tmp_path / "f.txt"
    target.write_text("v1")
    w, _ = _watcher(tmp_path)
    w.prime()
    target.write_text("v2-by-agent")
    w.note_self_write(str(target))
    assert asyncio.run(w.poll()) == []
    # A subsequent external change must surface (note was consumed). Use a
    # different-length payload so detection doesn't hinge on mtime resolution
    # (coarse on some filesystems, e.g. WSL2) when two writes share a size.
    target.write_text("v3-external-change")
    events = asyncio.run(w.poll())
    assert [e.change_type for e in events] == [MODIFIED]


def test_external_change_after_self_write_not_suppressed(tmp_path):
    """If the file diverges past our recorded signature, it's still reported."""
    target = tmp_path / "f.txt"
    target.write_text("v1")
    w, _ = _watcher(tmp_path)
    w.prime()
    target.write_text("v2-by-agent")
    w.note_self_write(str(target))
    # External actor changes it again before the poll -> signatures differ.
    target.write_text("v3-external-bigger")
    events = asyncio.run(w.poll())
    assert [e.change_type for e in events] == [MODIFIED]


def test_self_write_delete_suppressed(tmp_path):
    """A self-write that deletes the file is recorded and suppressed."""
    target = tmp_path / "f.txt"
    target.write_text("bye")
    w, _ = _watcher(tmp_path)
    w.prime()
    os.remove(target)
    w.note_self_write(str(target))
    assert asyncio.run(w.poll()) == []


def test_self_write_does_not_suppress_other_files(tmp_path):
    """Noting one path leaves changes to sibling files untouched."""
    mine = tmp_path / "mine.txt"
    other = tmp_path / "other.txt"
    mine.write_text("a")
    other.write_text("b")
    w, _ = _watcher(tmp_path)
    w.prime()
    mine.write_text("a-changed")
    w.note_self_write(str(mine))
    other.write_text("b-changed")
    events = asyncio.run(w.poll())
    assert [e.path for e in events] == [str(other)]


def test_is_running_reflects_lifecycle(tmp_path):
    w, _ = _watcher(tmp_path)
    assert w.is_running() is False

    async def scenario():
        w.start()
        assert w.is_running() is True
        await w.stop()
        assert w.is_running() is False

    asyncio.run(scenario())
