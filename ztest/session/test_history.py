#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the file-history read side (Phase 2).

Covers ``file_history`` (forward-scan grouping by path), ``diff_snapshot``
(unified diff of a before-image vs the current file), and ``restore`` (writing a
before-image back to disk, including the create-before-image = remove case).
"""
from __future__ import annotations

import os

from mote.contracts.fileops import CreateMutation, DeleteMutation, MutationSet, ReplaceMutation
from mote.runtime.fileops import FileOperations
from mote.runtime.fileops.artifact_budgets import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, encode_metadata_manifest
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.codec import iter_file_operations_events
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.history import SnapshotEntry, diff_snapshot, file_history, restore
from mote.runtime.session.log import SessionLog


class _TransactionRecorder:
    def __init__(self, log, root):
        self.root = root
        self.committed_sets = []
        self.operations = FileOperations(
            session_id=log.session_id,
            journal_path=log.path,
            get_project_root=lambda: str(root),
            flush_pending=log.writer.flush_inline,
            lock_root=root / "locks",
            event_sink=log.commit_offline,
            event_source=lambda: iter_file_operations_events(log.iter_events()),
        )

    def snapshot(self, path, *, tool=""):
        try:
            snapshot, raw = self.operations.capture(path)
        except FileNotFoundError:
            snapshot = None
            raw = b""
            maximum_bytes = len(encode_metadata_manifest(PreservedMetadata.for_create()))
        else:
            maximum_bytes = len(raw)
        scope = self.operations.artifacts.write_scope(
            owner="test-history-snapshot",
            maximum_bytes=maximum_bytes,
            ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
        )
        with scope:
            if snapshot is None:
                mutation = self.operations.mutation_factory.creation(
                    path,
                    raw,
                    scope=scope,
                )
            else:
                mutation = self.operations.mutation_factory.replacement(
                    snapshot,
                    raw,
                    scope=scope,
                )
            self.operations.mutations.commit(
                self.operations.mutation_factory.mutation_set(
                    source=tool,
                    mutations=(mutation,),
                ),
                ScopedMutationArtifacts(scope),
            )

    def restore(self, log, path, *, index=-1):
        before = {record.mutation_set.transaction_id for record in self.operations.journal.records()}
        result = restore(log, path, index=index)
        self.committed_sets.extend(
            record.mutation_set
            for record in self.operations.journal.records()
            if record.mutation_set.transaction_id not in before
        )
        return result


def _recorder(tmp_path):
    log = SessionLog("hist_sess", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="hist_sess"))
    return _TransactionRecorder(log, tmp_path), log


# ---------------------------------------------------------------------------
# file_history
# ---------------------------------------------------------------------------


def test_file_history_empty_when_no_events(tmp_path):
    _, log = _recorder(tmp_path)
    assert file_history(log) == {}


def test_file_history_groups_by_path_in_order(tmp_path):
    rec, log = _recorder(tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a1")
    rec.snapshot(str(a), tool="Write")
    a.write_text("a2")
    rec.snapshot(str(a), tool="Edit")
    b.write_text("b1")
    rec.snapshot(str(b), tool="Write")

    hist = file_history(log)
    assert set(hist) == {str(a), str(b)}
    assert [e.index for e in hist[str(a)]] == [0, 1]
    assert [e.tool for e in hist[str(a)]] == ["Write", "Edit"]
    assert len(hist[str(b)]) == 1


def test_file_history_entry_fields(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("hello")
    rec.snapshot(str(target), tool="Write")

    entry = file_history(log)[str(target)][0]
    assert isinstance(entry, SnapshotEntry)
    assert entry.operation == "update"
    assert entry.pre_size == len("hello")
    assert entry.existed is True
    assert entry.pre_hash is not None
    assert entry.ts  # carries the event timestamp


def test_file_history_create_entry_not_existed(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "new.txt"  # never created
    rec.snapshot(str(target), tool="Write")

    entry = file_history(log)[str(target)][0]
    assert entry.operation == "create"
    assert entry.pre_hash is None
    assert entry.existed is False


# ---------------------------------------------------------------------------
# diff_snapshot
# ---------------------------------------------------------------------------


def test_diff_snapshot_shows_change(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("line1\nline2\n")
    rec.snapshot(str(target), tool="Edit")
    # Mutate after snapshot to simulate the tool's write.
    target.write_text("line1\nCHANGED\n")

    out = diff_snapshot(log, str(target))
    assert "-line2" in out
    assert "+CHANGED" in out


def test_diff_snapshot_no_change_returns_empty(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("same\n")
    rec.snapshot(str(target), tool="Edit")
    # File unchanged since snapshot.
    assert diff_snapshot(log, str(target)) == ""


def test_diff_snapshot_create_before_image_is_empty_side(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "new.txt"
    rec.snapshot(str(target), tool="Write")  # before-image = absent
    target.write_text("fresh content\n")

    out = diff_snapshot(log, str(target))
    assert "+fresh content" in out


def test_diff_snapshot_unknown_path_raises(tmp_path):
    _, log = _recorder(tmp_path)
    try:
        diff_snapshot(log, str(tmp_path / "nope"))
        assert False, "expected KeyError"
    except KeyError:
        pass


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_writes_before_image_back(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("original")
    rec.snapshot(str(target), tool="Edit")
    target.write_text("mutated")

    assert rec.restore(log, str(target)) is True
    assert target.read_text() == "original"
    assert len(rec.committed_sets) == 1
    assert isinstance(rec.committed_sets[0], MutationSet)
    assert len(rec.committed_sets[0].mutations) == 1
    assert isinstance(rec.committed_sets[0].mutations[0], ReplaceMutation)


def test_restore_create_before_image_removes_file(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "new.txt"
    rec.snapshot(str(target), tool="Write")  # before-image = absent
    target.write_text("created by tool")

    assert rec.restore(log, str(target)) is True
    assert not target.exists()
    assert len(rec.committed_sets) == 1
    assert isinstance(rec.committed_sets[0].mutations[0], DeleteMutation)


def test_restore_selects_index(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("v0")
    rec.snapshot(str(target), tool="Write")  # index 0 -> before-image "v0"
    target.write_text("v1")
    rec.snapshot(str(target), tool="Edit")  # index 1 -> before-image "v1"
    target.write_text("v2")

    assert rec.restore(log, str(target), index=0) is True
    assert target.read_text() == "v0"
    assert len(rec.committed_sets) == 1


def test_restore_missing_blob_returns_false(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("data")
    rec.snapshot(str(target), tool="Edit")
    # Delete the underlying blob so restore can't find the content.
    blobs_root = log.path.parent / "blobs"
    for p in blobs_root.rglob("*"):
        if p.is_file():
            os.remove(p)

    assert rec.restore(log, str(target)) is False
    assert rec.committed_sets == []


def test_restore_unknown_path_raises(tmp_path):
    rec, log = _recorder(tmp_path)
    try:
        rec.restore(log, str(tmp_path / "nope"))
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_restore_missing_live_file_uses_formal_create_mutation(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("original")
    rec.snapshot(str(target), tool="Edit")
    target.unlink()

    assert rec.restore(log, str(target)) is True

    assert target.read_text() == "original"
    assert len(rec.committed_sets) == 1
    mutation = rec.committed_sets[0].mutations[0]
    assert isinstance(mutation, CreateMutation)
    assert mutation.expected_version.name_identity
    assert mutation.project_identity
    assert mutation.metadata.size > 0
