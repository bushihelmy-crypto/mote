from __future__ import annotations

import codecs
import hashlib

import pytest

from mote.contracts.file import (
    ContinueReadRequest,
    EncodingRejectedError,
    EncodingSource,
    FileReadRangeError,
    ReadCursorError,
    ReadViewStatus,
    SearchOutputMode,
    TextReadRequest,
    TextViewMode,
)
from mote.runtime.fileops import text_sources as text_sources_module
from mote.ztest.fileops_factory import FileOperations


def _operations(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project, FileOperations(
        session_id="text-view",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )


def _read_text(operations, path, **kwargs):
    cursor = kwargs.pop("cursor", None)
    request = (
        ContinueReadRequest(
            cursor=cursor,
            limit=kwargs.pop("limit", None),
            **kwargs,
        )
        if cursor is not None
        else TextReadRequest(**kwargs)
    )
    return operations.read_view(path, request)


def test_utf8_text_view_reports_encoding_and_complete_page(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "utf8.txt"
    target.write_text("alpha\n目标", encoding="utf-8")

    view = _read_text(operations, str(target))

    assert view.mode == TextViewMode.TEXT
    assert view.lines == ("alpha", "目标")
    assert view.offset == 1
    assert view.total_lines == 2
    assert view.status == ReadViewStatus.COMPLETE
    assert view.next_offset is None
    assert view.snapshot.encoding is not None
    assert view.snapshot.encoding.label == "utf-8"
    assert view.snapshot.encoding.source == EncodingSource.UTF8


@pytest.mark.parametrize(
    ("name", "encoding", "text", "canonical"),
    [
        ("gbk.txt", "gbk", "GBK 目标", "gbk"),
        ("big5.txt", "big5", "Big5 目標", "big5"),
        ("shift-jis.txt", "shift_jis", "Shift-JIS 目標", "shift_jis"),
    ],
)
def test_explicit_legacy_encodings_round_trip(
    tmp_path,
    name,
    encoding,
    text,
    canonical,
):
    project, operations = _operations(tmp_path)
    target = project / name
    target.write_bytes(text.encode(encoding))

    view = _read_text(operations, str(target), encoding=encoding)

    assert view.lines == (text,)
    assert view.snapshot.encoding is not None
    assert view.snapshot.encoding.label == canonical
    assert view.snapshot.encoding.source == EncodingSource.EXPLICIT


@pytest.mark.parametrize(
    ("name", "bom", "encoding", "label"),
    [
        ("utf16-le.txt", codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"),
        ("utf16-be.txt", codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"),
        ("utf32-le.txt", codecs.BOM_UTF32_LE, "utf-32-le", "utf-32-le"),
        ("utf32-be.txt", codecs.BOM_UTF32_BE, "utf-32-be", "utf-32-be"),
    ],
)
def test_utf16_and_utf32_bom_are_authoritative(
    tmp_path,
    name,
    bom,
    encoding,
    label,
):
    project, operations = _operations(tmp_path)
    target = project / name
    target.write_bytes(bom + "first\nsecond".encode(encoding))

    view = _read_text(operations, str(target))

    assert view.lines == ("first", "second")
    assert view.snapshot.encoding is not None
    assert view.snapshot.encoding.label == label
    assert view.snapshot.encoding.bom == bom
    assert view.snapshot.encoding.source == EncodingSource.BOM


def test_empty_file_has_one_search_addressable_logical_line(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "empty.txt"
    target.write_bytes(b"")

    view = _read_text(operations, str(target))

    assert view.lines == ("",)
    assert view.total_lines == 1
    assert view.status == ReadViewStatus.COMPLETE
    assert view.next_offset is None


def test_trailing_newline_preserves_the_final_empty_logical_line(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "trailing.txt"
    target.write_bytes(b"first\r\nsecond\n")

    view = _read_text(operations, str(target))

    assert view.lines == ("first", "second", "")
    assert view.total_lines == 3


def test_default_page_is_bounded_and_resumable(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "large.txt"
    expected = tuple(f"line-{index:04d}" for index in range(1, 2_003))
    target.write_text("\n".join(expected), encoding="utf-8")

    first = _read_text(operations, str(target))
    zero_limit = _read_text(operations, str(target), limit=0)
    target.unlink()
    second = _read_text(operations, str(target), cursor=first.next_cursor)

    assert first.lines == expected[:2_000]
    assert first.total_lines == 2_002
    assert first.status == ReadViewStatus.PARTIAL
    assert first.next_offset == 2_001
    assert first.next_cursor is not None
    assert zero_limit.lines == first.lines
    assert zero_limit.next_offset == first.next_offset
    assert second.lines == expected[2_000:]
    assert second.offset == 2_001
    assert second.status == ReadViewStatus.COMPLETE
    assert second.next_offset is None
    assert second.next_cursor is None


def test_text_cursor_rejects_a_different_path_and_explicit_offset(tmp_path):
    project, operations = _operations(tmp_path)
    first_path = project / "first.txt"
    second_path = project / "second.txt"
    first_path.write_text("\n".join(str(index) for index in range(3)), encoding="utf-8")
    second_path.write_text("other", encoding="utf-8")
    first = _read_text(operations, str(first_path), limit=1)

    with pytest.raises(ReadCursorError, match="invalid"):
        _read_text(operations, str(second_path), cursor=first.next_cursor)
    with pytest.raises(TypeError):
        _read_text(
            operations,
            str(first_path),
            offset=2,
            cursor=first.next_cursor,
        )


def test_explicit_page_and_offset_past_end_are_deterministic(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "page.txt"
    target.write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    middle = _read_text(operations, str(target), offset=2, limit=2)
    past_end = _read_text(operations, str(target), offset=20, limit=2)

    assert middle.lines == ("two", "three")
    assert middle.next_offset == 4
    assert middle.status == ReadViewStatus.PARTIAL
    assert past_end.lines == ()
    assert past_end.offset == 20
    assert past_end.total_lines == 4
    assert past_end.next_offset is None
    assert past_end.status == ReadViewStatus.COMPLETE


def test_document_extraction_consumes_sealed_bytes_and_reports_document_mode(
    tmp_path,
    monkeypatch,
):
    project, operations = _operations(tmp_path)
    target = project / "report.docx"
    target.write_bytes(b"sealed document bytes")
    captured = []

    def extract(raw, suffix, *, budget):
        captured.append((raw, suffix))
        target.write_bytes(b"changed after capture")
        return "heading\nbody"

    monkeypatch.setattr(text_sources_module, "extract_document_bytes", extract)

    view = _read_text(operations, str(target))

    assert captured == [(b"sealed document bytes", ".docx")]
    assert view.mode == TextViewMode.DOCUMENT
    assert view.lines == ("heading", "body")
    assert view.snapshot.encoding is None
    assert view.snapshot.version.digest == hashlib.sha256(b"sealed document bytes").hexdigest()


def test_document_view_rejects_misleading_encoding_controls(tmp_path, monkeypatch):
    project, operations = _operations(tmp_path)
    target = project / "report.docx"
    target.write_bytes(b"sealed document bytes")
    monkeypatch.setattr(
        text_sources_module,
        "extract_document_bytes",
        lambda _raw, _suffix, *, budget: "text",
    )

    with pytest.raises(EncodingRejectedError, match="do not apply"):
        _read_text(operations, str(target), encoding="utf-8")


def test_search_line_number_can_be_used_directly_as_text_view_offset(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "shared-lines.txt"
    target.write_bytes(b"before\r\nneedle\rafter\n")

    result = operations.search(
        root=str(target),
        content="needle",
        output_mode=SearchOutputMode.CONTENT,
    )
    line_number = result.rows[0].line_number
    view = _read_text(operations, str(target), offset=line_number, limit=1)

    assert line_number == 2
    assert view.lines == ("needle",)


def test_text_view_rejects_ranges_beyond_the_typed_contract(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "range.txt"
    target.write_text("content", encoding="utf-8")

    with pytest.raises(FileReadRangeError, match="at most 10000") as exc_info:
        _read_text(operations, str(target), limit=10_001)
    assert exc_info.value.context == {"limit": 10_001, "maximum": 10_000}

    with pytest.raises(FileReadRangeError, match="at least 1"):
        _read_text(operations, str(target), offset=0)


def test_text_view_reads_the_sealed_artifact_not_the_reopened_source(
    tmp_path,
    monkeypatch,
):
    project, operations = _operations(tmp_path)
    target = project / "snapshot.txt"
    target.write_bytes(b"before\nsealed")
    original = operations.artifacts.read_bytes

    def mutate_after_capture(ref):
        target.write_bytes(b"after\nchanged")
        return original(ref)

    monkeypatch.setattr(operations.artifacts, "read_bytes", mutate_after_capture)

    view = _read_text(operations, str(target))

    assert view.lines == ("before", "sealed")
    assert view.snapshot.version.digest == hashlib.sha256(b"before\nsealed").hexdigest()
    assert target.read_bytes() == b"after\nchanged"
