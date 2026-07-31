from __future__ import annotations

import hashlib

import pytest

from mote.contracts.file import (
    ByteReadRequest,
    ByteViewMode,
    ContinueReadRequest,
    FileReadRangeError,
    ReadCursorKind,
    ReadViewStatus,
)
from mote.ztest.fileops_factory import FileOperations


def _operations(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project, FileOperations(
        session_id="byte-view",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )


def _read_bytes(operations, path, *, kind=ReadCursorKind.RAW, **kwargs):
    mode = ByteViewMode.RAW if kind == ReadCursorKind.RAW else ByteViewMode.HEX
    return operations.read_view(path, ByteReadRequest(mode=mode, **kwargs))


def test_raw_view_is_lossless_bounded_and_resumable(tmp_path):
    project, operations = _operations(tmp_path)
    raw = bytes(range(256)) * 20
    target = project / "payload.bin"
    target.write_bytes(raw)

    first = _read_bytes(operations, str(target))
    target.unlink()
    second = operations.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor),
    )

    assert first.data == raw[:4096]
    assert first.status == ReadViewStatus.PARTIAL
    assert first.next_offset == 4096
    assert first.next_cursor is not None
    assert second.data == raw[4096:]
    assert second.status == ReadViewStatus.COMPLETE
    assert second.next_offset is None


def test_hex_view_uses_absolute_offsets_and_ascii_gutter(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "payload.dat"
    target.write_bytes(b"\x00ABC\xffxyz")

    view = _read_bytes(
        operations,
        str(target),
        kind=ReadCursorKind.HEX,
        offset=1,
        limit=5,
    )

    assert view.data == b"ABC\xffx"
    assert view.text == "0000000000000001  41 42 43 ff 78                                   |ABC.x|"
    assert view.next_offset == 6


def test_byte_view_rejects_negative_and_unbounded_ranges(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"data")

    with pytest.raises(FileReadRangeError):
        _read_bytes(operations, str(target), offset=-1)
    with pytest.raises(FileReadRangeError):
        _read_bytes(
            operations,
            str(target),
            limit=1_024 * 1_024 + 1,
        )


def test_byte_view_reads_sealed_artifact_not_reopened_source(tmp_path, monkeypatch):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"before")
    original = operations.artifacts.read_range

    def mutate_after_capture(ref, *, offset, limit):
        target.write_bytes(b"after!")
        return original(ref, offset=offset, limit=limit)

    monkeypatch.setattr(operations.artifacts, "read_range", mutate_after_capture)
    view = _read_bytes(operations, str(target))

    assert view.data == b"before"
    assert view.snapshot.version.digest == hashlib.sha256(b"before").hexdigest()
    assert target.read_bytes() == b"after!"
