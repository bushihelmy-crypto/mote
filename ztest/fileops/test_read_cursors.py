from __future__ import annotations

import hashlib
import json

import pytest

from mote.contracts.file import (
    ByteReadRequest,
    ByteViewMode,
    ContinueReadRequest,
    PdfReadRequest,
    PdfViewMode,
    ReadCursorError,
)
from mote.ztest.fileops_factory import FileOperations

try:
    import fitz
except ImportError:
    fitz = None


def _operations(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project, FileOperations(
        session_id="read-cursor",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )


def _pdf(path, labels):
    assert fitz is not None
    document = fitz.open()
    for label in labels:
        page = document.new_page(width=300, height=200)
        page.insert_text((40, 80), label)
    document.save(path)
    document.close()


def test_raw_cursor_continues_the_same_snapshot_after_source_replacement(tmp_path):
    project, operations = _operations(tmp_path)
    original = b"0123456789abcdef"
    target = project / "payload.bin"
    target.write_bytes(original)

    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )
    target.write_bytes(b"replacement-data")
    second = operations.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor, limit=4),
    )

    assert first.data + second.data == original[:8]
    assert first.next_cursor is not None
    assert second.snapshot.version.digest == hashlib.sha256(original).hexdigest()
    assert second.snapshot == first.snapshot


def test_observed_snapshot_is_durable_across_file_operations_restart(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "observed.txt"
    target.write_bytes(b"durable observation")

    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW),
    )
    reopened = _operations(tmp_path)[1]

    assert reopened.observed(str(target)) == first.snapshot
    assert reopened.health().observed_snapshots == 1
    reopened.invalidate(str(target))
    assert reopened.observed(str(target)) is None


def test_hex_cursor_continues_the_same_snapshot_after_source_deletion(tmp_path):
    project, operations = _operations(tmp_path)
    original = b"abcdefgh"
    target = project / "payload.dat"
    target.write_bytes(original)

    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.HEX, limit=4),
    )
    target.unlink()
    reopened = FileOperations(
        session_id="read-cursor",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )
    second = reopened.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor, limit=4),
    )

    assert first.data + second.data == original
    assert second.snapshot == first.snapshot
    assert second.next_cursor is None
    assert second.text.startswith("0000000000000004")


def test_cursor_is_an_idempotent_immutable_position(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"0123456789ab")
    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )

    request = ContinueReadRequest(cursor=first.next_cursor, limit=4)
    page_a = operations.read_view(str(target), request)
    page_b = operations.read_view(str(target), request)

    assert page_a.data == page_b.data == b"4567"
    assert page_a.offset == page_b.offset == 4
    assert page_a.next_cursor == page_b.next_cursor


def test_continuation_resolves_the_cursor_manifest_once(tmp_path, monkeypatch):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"01234567")
    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )
    original_open = operations.read_cursors.open
    calls = []

    def counted_open(cursor):
        calls.append(cursor)
        return original_open(cursor)

    monkeypatch.setattr(operations.read_cursors, "open", counted_open)

    operations.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor, limit=4),
    )

    assert calls == [first.next_cursor]


@pytest.mark.skipif(fitz is None, reason="PDF cursor test requires PyMuPDF")
def test_pdf_cursor_survives_source_replacement_and_deletion(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target, ("original-1", "original-2", "original-3"))

    first = operations.read_view(
        str(target),
        PdfReadRequest(mode=PdfViewMode.RENDER),
    )
    _pdf(project / "replacement.pdf", ("replacement",))
    (project / "replacement.pdf").replace(target)
    second = operations.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor),
    )
    target.unlink()
    third = operations.read_view(
        str(target),
        ContinueReadRequest(cursor=second.next_cursor),
    )

    assert [
        first.pages[0].page_number,
        second.pages[0].page_number,
        third.pages[0].page_number,
    ] == [1, 2, 3]
    assert second.snapshot == first.snapshot
    assert third.snapshot == first.snapshot
    assert first.pages[0].png != second.pages[0].png
    assert second.pages[0].png != third.pages[0].png
    assert third.next_cursor is None


@pytest.mark.skipif(fitz is None, reason="PDF cursor test requires PyMuPDF")
def test_pdf_default_pagination_honors_the_read_page_limit(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target, ("page-1", "page-2", "page-3"))

    first = operations.read_view(
        str(target),
        PdfReadRequest(mode=PdfViewMode.RENDER, limit=2),
    )
    second = operations.read_view(
        str(target),
        ContinueReadRequest(cursor=first.next_cursor, limit=1),
    )

    assert [page.page_number for page in first.pages] == [1, 2]
    assert [page.page_number for page in second.pages] == [3]
    assert second.next_cursor is None


def test_cursor_tag_rejects_cross_mode_reuse(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"01234567")
    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )
    opened = operations.read_cursors.open(first.next_cursor)

    with pytest.raises(ReadCursorError, match="kind|mode|manifest"):
        operations.byte_views.read(
            str(target),
            mode=ByteViewMode.HEX,
            continuation=opened,
            expected_epoch=operations.cursor_registry.current_epoch,
        )


def test_typed_read_requests_make_irrelevant_selectors_unrepresentable():
    with pytest.raises(TypeError):
        ByteReadRequest(mode=ByteViewMode.RAW, pages="1")
    with pytest.raises(TypeError):
        ContinueReadRequest(cursor="opaque", offset=4)
    with pytest.raises(TypeError):
        ContinueReadRequest(cursor="opaque", dpi=300)


def test_cursor_rejects_a_different_requested_path(tmp_path):
    project, operations = _operations(tmp_path)
    first_path = project / "first.bin"
    second_path = project / "second.bin"
    first_path.write_bytes(b"01234567")
    second_path.write_bytes(b"01234567")
    first = operations.read_view(
        str(first_path),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )

    with pytest.raises(ReadCursorError, match="invalid"):
        operations.read_view(
            str(second_path),
            ContinueReadRequest(cursor=first.next_cursor),
        )


def test_cursor_capability_rejects_tampering(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "payload.bin"
    target.write_bytes(b"01234567")
    first = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )
    replacement = "A" if first.next_cursor[-1] != "A" else "B"
    tampered = f"{first.next_cursor[:-1]}{replacement}"

    with pytest.raises(ReadCursorError, match="invalid"):
        operations.read_view(
            str(target),
            ContinueReadRequest(cursor=tampered),
        )


def test_oversized_cursor_is_rejected_before_registry_access(tmp_path):
    _, operations = _operations(tmp_path)

    with pytest.raises(ReadCursorError, match="cursor"):
        operations.read_cursors.open("A" * 100_000)


def test_cursor_rejects_a_large_known_blob_before_reading_it_as_a_manifest(
    tmp_path,
    monkeypatch,
):
    project, operations = _operations(tmp_path)
    target = project / "large.bin"
    target.write_bytes(b"x" * (2 * 1_024 * 1_024))
    snapshot, _ = operations.capture(str(target))
    forged = operations.cursor_registry.issue(
        namespace="read",
        root_manifest=snapshot.artifact,
        pinned_artifacts=(snapshot.metadata,),
        position=0,
        expected_epoch=operations.cursor_registry.current_epoch,
    )
    original_read = operations.artifacts.read_bytes
    attempted = []

    def guarded_read(ref):
        if ref == snapshot.artifact:
            attempted.append(ref)
            raise AssertionError("oversized non-manifest blob was read")
        return original_read(ref)

    monkeypatch.setattr(operations.artifacts, "read_bytes", guarded_read)

    with pytest.raises(ReadCursorError, match="manifest"):
        operations.read_view(
            str(target),
            ContinueReadRequest(cursor=forged),
        )
    assert attempted == []


@pytest.mark.skipif(fitz is None, reason="PDF cursor test requires PyMuPDF")
def test_pdf_cursor_revalidates_manifest_dpi_before_rendering(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target, ("page-1", "page-2"))
    first = operations.read_view(
        str(target),
        PdfReadRequest(mode=PdfViewMode.RENDER),
    )
    opened = operations.read_cursors.open(first.next_cursor)
    access = operations.cursor_registry.open(
        first.next_cursor,
        expected_namespace="read",
    )
    manifest = opened.manifest
    manifest_payload = json.loads(operations.artifacts.read_bytes(manifest).decode("utf-8"))
    manifest_payload["payload"]["dpi"] = 100_000
    raw = json.dumps(
        manifest_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with operations.artifacts.write_scope(
        owner="test-forged-pdf-cursor",
        maximum_bytes=len(raw),
        ttl_seconds=60,
    ) as scope:
        forged_manifest = scope.put_bytes(raw)
        forged = operations.cursor_registry.issue(
            namespace="read",
            root_manifest=forged_manifest,
            pinned_artifacts=access.lease.pinned_artifacts,
            position=opened.position,
            expected_epoch=operations.cursor_registry.current_epoch,
        )
        scope.complete(durability_root=operations.cursor_registry.path.parent)

    with pytest.raises(ReadCursorError, match="DPI|manifest"):
        operations.read_view(
            str(target),
            ContinueReadRequest(cursor=forged),
        )
