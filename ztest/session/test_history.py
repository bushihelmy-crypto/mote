#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the file-history read side (Phase 2).

Covers ``file_history`` (forward-scan grouping by path), ``diff_snapshot``
(unified diff of a before-image vs the current file), and ``restore`` (writing a
before-image back to disk, including the create-before-image = remove case).
"""
from __future__ import annotations

import os

from metagpt.session.history import (
    SnapshotEntry,
    diff_snapshot,
    file_history,
    restore,
)
from metagpt.session.log import SessionLog
from metagpt.session.snapshot import FileSnapshotRecorder


def _recorder(tmp_path):
    log = SessionLog("hist_sess", base_dir=str(tmp_path))
    return FileSnapshotRecorder(log), log


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

    assert restore(log, str(target)) is True
    assert target.read_text() == "original"


def test_restore_create_before_image_removes_file(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "new.txt"
    rec.snapshot(str(target), tool="Write")  # before-image = absent
    target.write_text("created by tool")

    assert restore(log, str(target)) is True
    assert not target.exists()


def test_restore_selects_index(tmp_path):
    rec, log = _recorder(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("v0")
    rec.snapshot(str(target), tool="Write")  # index 0 -> before-image "v0"
    target.write_text("v1")
    rec.snapshot(str(target), tool="Edit")  # index 1 -> before-image "v1"
    target.write_text("v2")

    assert restore(log, str(target), index=0) is True
    assert target.read_text() == "v0"


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

    assert restore(log, str(target)) is False


def test_restore_unknown_path_raises(tmp_path):
    _, log = _recorder(tmp_path)
    try:
        restore(log, str(tmp_path / "nope"))
        assert False, "expected KeyError"
    except KeyError:
        pass
