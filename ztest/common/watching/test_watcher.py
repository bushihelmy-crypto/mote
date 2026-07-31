#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the FileWatcher core (Phase 3).

Drives :meth:`FileWatcher.poll` directly (no async loop) to verify create /
modify / delete detection, baseline priming, and ignore-pattern pruning.
"""
from __future__ import annotations

import asyncio
import os
import threading

import pytest

from mote.contracts.events.file.observation import FileChangedEvent
from mote.contracts.file.identity import FileChangeKind, PresentVersion
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.watching.watcher import FileWatcher
from mote.ztest.fileops_factory import FileOperations


def _collect():
    """Return (on_change coroutine, captured-list) recording every event."""
    captured: list[FileChangedEvent] = []

    async def on_change(event: FileChangedEvent) -> None:
        captured.append(event)

    return on_change, captured


def _watcher(tmp_path, **kw):
    on_change, captured = _collect()
    state = tmp_path.parent / f"{tmp_path.name}-watch-state"
    operations = FileOperations(
        session_id=tmp_path.name,
        journal_path=state / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=state / "locks",
    )
    return FileWatcher([str(tmp_path)], on_change, operations, **kw), captured


@pytest.mark.asyncio
async def test_unprimed_first_poll_emits_created_for_existing(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    w, captured = _watcher(tmp_path)
    events = await w.poll()
    assert {e.change_type for e in events} == {FileChangeKind.CREATED}
    assert [e.path for e in captured] == [str(tmp_path / "a.txt")]


@pytest.mark.asyncio
async def test_prime_suppresses_initial_burst(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    w, captured = _watcher(tmp_path)
    w.prime()
    events = await w.poll()
    assert events == []
    assert captured == []


@pytest.mark.asyncio
async def test_detects_new_file_after_prime(tmp_path):
    w, captured = _watcher(tmp_path)
    w.prime()
    (tmp_path / "new.txt").write_text("hello")
    events = await w.poll()
    assert len(events) == 1
    assert events[0].change_type is FileChangeKind.CREATED
    assert events[0].path == str(tmp_path / "new.txt")
    assert isinstance(events[0].version, PresentVersion)
    assert events[0].version.size == len("hello")


@pytest.mark.asyncio
async def test_detects_modification(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("v1")
    w, _ = _watcher(tmp_path)
    w.prime()
    # Bump mtime deterministically (size also changes).
    target.write_text("v2-longer")
    events = await w.poll()
    assert len(events) == 1
    assert events[0].change_type is FileChangeKind.MODIFIED


@pytest.mark.asyncio
async def test_detects_same_size_modification_via_mtime(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa")
    w, _ = _watcher(tmp_path)
    w.prime()
    # Same size, different mtime -> still a modification (mtime_ns in signature).
    os.utime(target, ns=(10**18, 10**18))
    events = await w.poll()
    assert [e.change_type for e in events] == [FileChangeKind.MODIFIED]


@pytest.mark.asyncio
async def test_detects_digest_change_when_size_and_mtime_are_restored(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa")
    original = os.stat(target)
    w, _ = _watcher(tmp_path)
    w.prime()
    target.write_text("bbb")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    events = await w.poll()

    assert [event.change_type for event in events] == [FileChangeKind.MODIFIED]
    assert events[0].prior_version.digest != events[0].version.digest


@pytest.mark.asyncio
async def test_detects_deletion(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("bye")
    w, _ = _watcher(tmp_path)
    w.prime()
    os.remove(target)
    events = await w.poll()
    assert len(events) == 1
    assert events[0].change_type is FileChangeKind.DELETED
    assert events[0].path == str(target)


@pytest.mark.asyncio
async def test_no_changes_emits_nothing(tmp_path):
    (tmp_path / "f.txt").write_text("stable")
    w, _ = _watcher(tmp_path)
    w.prime()
    assert await asyncio.wait_for(w.poll(), timeout=5) == []


@pytest.mark.asyncio
async def test_poll_probes_off_the_event_loop_thread(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("stable")
    w, _ = _watcher(tmp_path)
    w.prime()
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    original = w._file_changes.probe_file_version

    def record_thread(path, *, prior=None):
        probe_threads.append(threading.get_ident())
        return original(path, prior=prior)

    monkeypatch.setattr(w._file_changes, "probe_file_version", record_thread)

    assert await w.poll() == []
    assert probe_threads
    assert all(thread_id != main_thread for thread_id in probe_threads)


@pytest.mark.asyncio
async def test_concurrent_polls_emit_one_transition(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("before")
    w, captured = _watcher(tmp_path)
    w.prime()
    target.write_text("external-after")

    first, second = await asyncio.gather(w.poll(), w.poll())

    assert len(first) + len(second) == 1
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_ignore_prunes_matching_files(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.pyc").write_text("s")
    w, _ = _watcher(tmp_path, ignore=["*.pyc"])
    events = await asyncio.wait_for(w.poll(), timeout=5)
    paths = {e.path for e in events}
    assert str(tmp_path / "keep.txt") in paths
    assert str(tmp_path / "skip.pyc") not in paths


@pytest.mark.asyncio
async def test_ignore_prunes_directories(tmp_path):
    sub = tmp_path / ".git"
    sub.mkdir()
    (sub / "config").write_text("ignored")
    (tmp_path / "real.txt").write_text("seen")
    w, _ = _watcher(tmp_path, ignore=[".git"])
    events = await w.poll()
    paths = {e.path for e in events}
    assert str(tmp_path / "real.txt") in paths
    assert all(".git" not in p for p in paths)


@pytest.mark.asyncio
async def test_watches_single_file_root(tmp_path):
    target = tmp_path / "only.txt"
    target.write_text("a")
    on_change, captured = _collect()
    state = tmp_path.parent / f"{tmp_path.name}-single-state"
    operations = FileOperations(
        session_id=f"{tmp_path.name}-single",
        journal_path=state / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=state / "locks",
    )
    w = FileWatcher([str(target)], on_change, operations)
    w.prime()
    target.write_text("bb")
    events = await w.poll()
    assert [e.change_type for e in events] == [FileChangeKind.MODIFIED]


@pytest.mark.asyncio
async def test_multiple_changes_in_one_poll(tmp_path):
    keep = tmp_path / "keep.txt"
    gone = tmp_path / "gone.txt"
    keep.write_text("1")
    gone.write_text("2")
    w, _ = _watcher(tmp_path)
    w.prime()
    keep.write_text("1-changed")
    os.remove(gone)
    (tmp_path / "added.txt").write_text("3")
    events = await w.poll()
    by_type = {e.change_type for e in events}
    assert by_type == {
        FileChangeKind.CREATED,
        FileChangeKind.MODIFIED,
        FileChangeKind.DELETED,
    }


@pytest.mark.asyncio
async def test_durable_commit_suppresses_before_file_mutated_event_arrives(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"before")
    w, _ = _watcher(tmp_path)
    operations = w._file_changes
    snapshot, _ = operations.capture(str(target))
    w.prime()
    with operations.artifacts.write_scope(
        owner="watcher-managed-transition-test",
        maximum_bytes=len(b"managed-after"),
        ttl_seconds=60,
    ) as scope:
        mutation = operations.mutation_factory.replacement(
            snapshot,
            b"managed-after",
            scope=scope,
        )
        mutation_set = operations.mutation_factory.mutation_set(
            source="test-managed-write",
            mutations=(mutation,),
        )
        operations.mutations.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )

    second_snapshot, _ = operations.capture(str(target))
    with operations.artifacts.write_scope(
        owner="watcher-managed-transition-test-2",
        maximum_bytes=len(b"managed-after-2"),
        ttl_seconds=60,
    ) as scope:
        second_mutation = operations.mutation_factory.replacement(
            second_snapshot,
            b"managed-after-2",
            scope=scope,
        )
        second_set = operations.mutation_factory.mutation_set(
            source="test-managed-write-2",
            mutations=(second_mutation,),
        )
        operations.mutations.commit(
            second_set,
            ScopedMutationArtifacts(scope),
        )

    assert await asyncio.wait_for(w.poll(), timeout=5) == []

    target.write_bytes(b"external-after")
    events = await asyncio.wait_for(w.poll(), timeout=5)
    assert [event.change_type for event in events] == [FileChangeKind.MODIFIED]


@pytest.mark.asyncio
async def test_batch_changes_scan_durable_journal_once(tmp_path, monkeypatch):
    paths = []
    for index in range(20):
        path = tmp_path / f"file-{index}.txt"
        path.write_text("before")
        paths.append(path)
    w, _ = _watcher(tmp_path)
    operations = w._file_changes
    w.prime()
    calls = 0
    original = operations.journal.records

    def counted_records():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(operations.journal, "records", counted_records)
    for path in paths:
        path.write_text("external-after")

    events = await w.poll()

    assert len(events) == len(paths)
    assert calls == 1


@pytest.mark.asyncio
async def test_is_running_reflects_lifecycle(tmp_path):
    w, _ = _watcher(tmp_path)
    assert w.is_running() is False

    async def scenario():
        await w.start_async()
        assert w.is_running() is True
        await w.stop()
        assert w.is_running() is False

    await scenario()
