from __future__ import annotations

import base64
import codecs
import json
import os

import pytest

from mote.contracts.file import ContentChangedError, SearchCursorError, SearchOutputMode, SearchSkipReason
from mote.runtime.fileops import text_sources as text_sources_module
from mote.ztest.fileops_factory import FileOperations


def _operations(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    operations = FileOperations(
        session_id="search",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )
    return project, operations


def test_count_is_occurrences_for_plain_text(tmp_path):
    project, operations = _operations(tmp_path)
    (project / "three.txt").write_text("hit hit hit\n", encoding="utf-8")

    result = operations.search(
        root=str(project),
        content="hit",
        output_mode=SearchOutputMode.COUNT,
    )

    assert result.rows[0].occurrence_count == 3
    assert result.summary.total_occurrences == 3
    assert result.summary.matched_files == 1


@pytest.mark.parametrize(
    ("name", "encoding", "raw"),
    [
        ("gbk.txt", "gbk", "目标".encode("gbk")),
        ("big5.txt", "big5", "目標".encode("big5")),
        ("shift-jis.txt", "shift_jis", "目標".encode("shift_jis")),
        ("utf16.txt", None, codecs.BOM_UTF16_LE + "目标".encode("utf-16-le")),
    ],
)
def test_search_preserves_explicit_and_bom_encodings(tmp_path, name, encoding, raw):
    project, operations = _operations(tmp_path)
    (project / name).write_bytes(raw)

    result = operations.search(
        root=str(project / name),
        content="目",
        encoding=encoding,
        output_mode=SearchOutputMode.ONLY_MATCHING,
    )

    assert result.summary.total_occurrences == 1
    assert result.rows[0].matched_text.startswith("目")


def test_nul_without_bom_or_explicit_encoding_is_reported_as_binary(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "binary.dat"
    target.write_bytes(b"needle\0payload")

    result = operations.search(root=str(target), content="needle")

    assert result.rows == ()
    assert result.skipped[0].reason == SearchSkipReason.BINARY
    assert result.summary.skipped_files == 1


def test_skipped_report_is_streamed_with_a_bounded_preview(tmp_path):
    project, operations = _operations(tmp_path)
    for index in range(105):
        (project / f"binary-{index:03d}.dat").write_bytes(b"needle\0payload")

    result = operations.search(root=str(project), content="needle")

    assert result.summary.skipped_files == 105
    assert len(result.skipped) == 100
    assert result.skipped_truncated
    assert sum(1 for _ in operations.artifacts.iter_lines(result.skipped_artifact)) == 105


def test_document_and_text_use_same_occurrence_semantics(tmp_path, monkeypatch):
    project, operations = _operations(tmp_path)
    (project / "plain.txt").write_text("hit hit", encoding="utf-8")
    document = project / "report.docx"
    document.write_bytes(b"sealed bytes")
    captured = []

    def extract(raw, suffix, *, budget):
        captured.append((raw, suffix))
        document.write_bytes(b"changed after capture")
        return "hit hit"

    monkeypatch.setattr(text_sources_module, "extract_document_bytes", extract)
    result = operations.search(
        root=str(project),
        content="hit",
        output_mode=SearchOutputMode.COUNT,
    )

    counts = {os.path.basename(row.path.display): row.occurrence_count for row in result.rows}
    assert counts == {"plain.txt": 2, "report.docx": 2}
    assert result.summary.total_occurrences == 4
    assert captured == [(b"sealed bytes", ".docx")]


def test_content_context_and_only_matching_are_structured(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "context.txt"
    target.write_text("before\nhit hit\nafter\n", encoding="utf-8")

    content = operations.search(
        root=str(target),
        content="hit",
        output_mode=SearchOutputMode.CONTENT,
        before_context=1,
        after_context=1,
    )
    matching = operations.search(
        root=str(target),
        content="hit",
        output_mode=SearchOutputMode.ONLY_MATCHING,
    )

    assert [(row.line_number, row.is_context) for row in content.rows] == [
        (1, True),
        (2, False),
        (3, True),
    ]
    assert [row.matched_text for row in matching.rows] == ["hit", "hit"]


def test_summary_scans_all_rows_before_stable_cursor_pagination(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "rows.txt"
    target.write_text("hit one\nhit two\nhit three\n", encoding="utf-8")

    first = operations.search(
        root=str(project),
        content="hit",
        output_mode=SearchOutputMode.CONTENT,
        limit=1,
    )
    target.write_text("replacement", encoding="utf-8")
    second = operations.search(root=str(project), cursor=first.next_cursor, limit=1)

    assert first.summary.total_occurrences == 3
    assert len(first.rows) == 1
    assert first.next_cursor is not None
    assert second.rows[0].text == "hit two"
    assert second.artifact == first.artifact
    assert second.output_mode == SearchOutputMode.CONTENT


def test_default_page_is_bounded_and_cursor_covers_remaining_rows(tmp_path):
    project, operations = _operations(tmp_path)
    for index in range(1_002):
        (project / f"row-{index:04d}.txt").write_bytes(b"x")

    first = operations.search(root=str(project), files="*.txt")
    second = operations.search(root=str(project), cursor=first.next_cursor)

    assert len(first.rows) == 1_000
    assert first.status.value == "partial"
    assert len(second.rows) == 2
    assert second.status.value == "complete"
    assert first.summary.discovered_files == 1_002


def test_tampered_or_missing_cursor_is_typed(tmp_path):
    project, operations = _operations(tmp_path)
    (project / "rows.txt").write_text("hit\nhit\n", encoding="utf-8")
    first = operations.search(root=str(project), content="hit", limit=1)

    with pytest.raises(SearchCursorError):
        operations.search(root=str(project), cursor=f"{first.next_cursor}x")

    traversal = base64.urlsafe_b64encode(
        json.dumps(
            {
                "format_version": 1,
                "digest": "../../outside",
                "size": 1,
                "offset": 0,
            }
        ).encode("ascii")
    ).decode("ascii")
    with pytest.raises(SearchCursorError):
        operations.search(root=str(project), cursor=traversal)


def test_colon_and_non_utf8_paths_round_trip_without_parsing(tmp_path):
    if os.name != "posix":
        pytest.skip("non-UTF-8 path spelling is a POSIX test")
    project, operations = _operations(tmp_path)
    colon = project / "name:part.txt"
    colon.write_text("hit", encoding="utf-8")
    raw_path = os.fsencode(project) + b"/invalid-\xff.txt"
    with open(raw_path, "wb") as stream:
        stream.write(b"hit")

    result = operations.search(root=str(project), content="hit")

    native = {os.fsencode(path.native) for path in result.files}
    assert os.fsencode(colon) in native
    assert raw_path in native


def test_changed_file_is_reported_and_does_not_mix_versions(tmp_path, monkeypatch):
    project, operations = _operations(tmp_path)
    target = project / "changing.txt"
    target.write_text("hit", encoding="utf-8")

    def changed(*args, **kwargs):
        raise ContentChangedError("changed during search")

    monkeypatch.setattr(operations.reader, "open_snapshot", changed)
    result = operations.search(root=str(target), content="hit")

    assert result.rows == ()
    assert result.skipped[0].reason == SearchSkipReason.CHANGED


def test_gitignore_and_path_order_are_candidate_discovery_semantics(tmp_path):
    project, operations = _operations(tmp_path)
    (project / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (project / "z.txt").write_text("hit", encoding="utf-8")
    (project / "a.txt").write_text("hit", encoding="utf-8")
    (project / "ignored.txt").write_text("hit", encoding="utf-8")

    result = operations.search(root=str(project), content="hit")

    assert [os.path.basename(path.display) for path in result.files] == [
        "a.txt",
        "z.txt",
    ]
