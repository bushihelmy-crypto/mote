#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the file-history snapshot subsystem (Phase 1).

Covers the content-addressed :class:`BlobStore` (dedup + atomic write), the
:class:`FileSnapshotRecorder` (before-image capture, create vs update, disabled
gate, best-effort), conformance to the ``FileSnapshotStore`` protocol, that
``replay`` ignores snapshot events, and end-to-end capture through the Write /
Edit tools.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from mote.session.events import FILE_SNAPSHOT
from mote.session.log import SessionLog
from mote.session.replay import replay
from mote.session.snapshot import BlobStore, FileSnapshotRecorder

# ---------------------------------------------------------------------------
# BlobStore
# ---------------------------------------------------------------------------


def test_blobstore_put_returns_sha256_and_roundtrips(tmp_path):
    store = BlobStore(tmp_path)
    content = b"hello world"
    digest = store.put(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert store.exists(digest)
    assert store.get(digest) == content


def test_blobstore_dedups_identical_content(tmp_path):
    store = BlobStore(tmp_path)
    d1 = store.put(b"same")
    d2 = store.put(b"same")
    assert d1 == d2
    # Exactly one blob file on disk for identical content.
    blob_files = list((tmp_path / "blobs").rglob("*"))
    blob_files = [p for p in blob_files if p.is_file()]
    assert len(blob_files) == 1


def test_blobstore_get_missing_returns_none(tmp_path):
    store = BlobStore(tmp_path)
    assert store.get("deadbeef") is None


def test_blobstore_leaves_no_tmp_files(tmp_path):
    store = BlobStore(tmp_path)
    store.put(b"abc")
    leftover = [p for p in (tmp_path / "blobs").rglob("*.tmp.*")]
    assert leftover == []


# ---------------------------------------------------------------------------
# FileSnapshotRecorder
# ---------------------------------------------------------------------------


def _recorder(tmp_path, **kw):
    log = SessionLog("snap_sess", base_dir=str(tmp_path))
    return FileSnapshotRecorder(log, **kw), log


def test_snapshot_existing_file_records_update_with_prehash(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"before")
    rec, log = _recorder(tmp_path)

    rec.snapshot(str(target), tool="Write")

    records = list(log.iter_raw())
    assert len(records) == 1
    payload = records[0]["payload"]
    assert records[0]["type"] == FILE_SNAPSHOT
    assert payload["operation"] == "update"
    assert payload["pre_hash"] == hashlib.sha256(b"before").hexdigest()
    assert payload["pre_size"] == len(b"before")
    assert payload["tool"] == "Write"
    # The before-image is retrievable from the blob store.
    assert rec.blobs.get(payload["pre_hash"]) == b"before"


def test_snapshot_missing_file_records_create_without_hash(tmp_path):
    rec, log = _recorder(tmp_path)
    rec.snapshot(str(tmp_path / "new.txt"), tool="Write")

    payload = list(log.iter_raw())[0]["payload"]
    assert payload["operation"] == "create"
    assert payload["pre_hash"] is None
    assert payload["pre_size"] == 0


def test_disabled_recorder_records_nothing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"x")
    rec, log = _recorder(tmp_path, enabled=False)
    rec.snapshot(str(target))
    assert list(log.iter_raw()) == []


def test_snapshot_unreadable_path_is_best_effort(tmp_path):
    # A directory is not readable as bytes -> swallowed, no event, no raise.
    rec, log = _recorder(tmp_path)
    d = tmp_path / "adir"
    d.mkdir()
    rec.snapshot(str(d))
    assert list(log.iter_raw()) == []


def test_recorder_conforms_to_protocol(tmp_path):
    from mote.common.interface import FileSnapshotStore

    rec, _ = _recorder(tmp_path)
    assert isinstance(rec, FileSnapshotStore)


def test_repeated_snapshots_dedup_blob(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"same-content")
    rec, log = _recorder(tmp_path)
    rec.snapshot(str(target))
    rec.snapshot(str(target))
    # Two events, one blob (content unchanged).
    assert len(list(log.iter_raw())) == 2
    blobs = [p for p in (log.path.parent / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 1


# ---------------------------------------------------------------------------
# replay ignores snapshot events
# ---------------------------------------------------------------------------


def test_replay_ignores_file_snapshot_events(tmp_path):
    from mote.common.schema import UserMessage
    from mote.session.events import MessageEvent

    log = SessionLog("mix", base_dir=str(tmp_path))
    log.append(MessageEvent(message=UserMessage(content="hi")))
    rec = FileSnapshotRecorder(log)
    target = tmp_path / "f.txt"
    target.write_bytes(b"data")
    rec.snapshot(str(target))

    result = replay(log)
    assert [m.content for m in result.messages] == ["hi"]
    assert result.message_events == 1


# ---------------------------------------------------------------------------
# End-to-end through the Edit tool (whole-file write + substring edit)
# ---------------------------------------------------------------------------


def _bind_snapshot(tool, rec):
    """Inject the snapshot + file-read capabilities the way bind() would."""
    read_state: dict[str, int] = {}
    tool.record_file_snapshot = lambda full_path, *, tool="": rec.snapshot(full_path, tool=tool)
    tool.record_file_read = lambda path, mtime: read_state.__setitem__(path, mtime)
    tool.get_file_read_mtime = lambda path: read_state.get(path)
    return read_state


def test_write_overwrite_captures_before_image(tmp_path):
    # Whole-file overwrite = Edit with an empty old_string (the former Write).
    from mote.executor.tools.edit import Edit

    target = tmp_path / "f.txt"
    target.write_text("original")

    rec, log = _recorder(tmp_path)
    tool = Edit()
    read_state = _bind_snapshot(tool, rec)
    # Mark the file as read this session so read-before-overwrite passes.
    read_state[str(target)] = os.stat(target).st_mtime_ns

    asyncio.run(tool.call(file_path=str(target), old_string="", new_string="replacement"))

    assert target.read_text() == "replacement"
    snaps = [r for r in log.iter_raw() if r["type"] == FILE_SNAPSHOT]
    assert len(snaps) == 1
    assert snaps[0]["payload"]["pre_hash"] == hashlib.sha256(b"original").hexdigest()
    assert snaps[0]["payload"]["tool"] == "Edit"


def test_write_new_file_records_create(tmp_path):
    from mote.executor.tools.edit import Edit

    target = tmp_path / "new.txt"
    rec, log = _recorder(tmp_path)
    tool = Edit()
    _bind_snapshot(tool, rec)

    asyncio.run(tool.call(file_path=str(target), old_string="", new_string="fresh"))

    snaps = [r for r in log.iter_raw() if r["type"] == FILE_SNAPSHOT]
    assert len(snaps) == 1
    assert snaps[0]["payload"]["operation"] == "create"
    assert snaps[0]["payload"]["pre_hash"] is None


def test_edit_captures_before_image(tmp_path):
    from mote.executor.tools.edit import Edit

    target = tmp_path / "f.py"
    target.write_text("a = 1\nb = 2\n")

    rec, log = _recorder(tmp_path)
    tool = Edit()
    read_state = _bind_snapshot(tool, rec)
    read_state[str(target)] = os.stat(target).st_mtime_ns

    asyncio.run(tool.call(file_path=str(target), old_string="a = 1", new_string="a = 99"))

    assert "a = 99" in target.read_text()
    snaps = [r for r in log.iter_raw() if r["type"] == FILE_SNAPSHOT]
    assert len(snaps) == 1
    assert snaps[0]["payload"]["pre_hash"] == hashlib.sha256(b"a = 1\nb = 2\n").hexdigest()


def test_unbound_tool_snapshot_is_noop(tmp_path):
    # A tool used standalone (no Role injected the capability) must not blow up.
    from mote.executor.tools.edit import Edit

    tool = Edit()
    # _snapshot_pre_write self-skips when the capability is absent.
    tool._snapshot_pre_write(str(tmp_path / "x"))
