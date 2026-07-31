from __future__ import annotations

import pytest

from mote.contracts.file import PdfProcessingError, PdfReadRequest, PdfViewMode, ReadCursorKind, ReadViewStatus
from mote.ztest.fileops_factory import FileOperations

fitz = pytest.importorskip("fitz")


def _operations(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project, FileOperations(
        session_id="pdf-view",
        journal_path=tmp_path / "state" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )


def _read_pdf(operations, path, *, kind=ReadCursorKind.PDF_TEXT, **kwargs):
    mode = PdfViewMode.TEXT if kind == ReadCursorKind.PDF_TEXT else PdfViewMode.RENDER
    return operations.read_view(path, PdfReadRequest(mode=mode, **kwargs))


def _pdf(path, pages=3):
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=300, height=200)
        page.insert_text((40, 80), f"page-{number}")
    document.save(path)
    document.close()


def test_pdf_text_selection_preserves_page_provenance(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target)

    view = _read_pdf(
        operations,
        str(target),
        pages="2-3",
    )

    assert view.total_pages == 3
    assert [page.page_number for page in view.pages] == [2, 3]
    assert "page-2" in view.pages[0].text
    assert "page-3" in view.pages[1].text
    assert view.status == ReadViewStatus.COMPLETE


def test_pdf_render_returns_png_with_dimensions_and_default_cursor(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target)

    view = _read_pdf(
        operations,
        str(target),
        kind=ReadCursorKind.PDF_RENDER,
    )

    assert len(view.pages) == 1
    assert view.pages[0].png.startswith(b"\x89PNG\r\n\x1a\n")
    assert view.pages[0].width == 600
    assert view.pages[0].height == 400
    assert view.status == ReadViewStatus.PARTIAL
    assert view.next_pages == "2"
    assert view.next_cursor is not None


def test_pdf_page_selection_is_strict_and_bounded(tmp_path):
    project, operations = _operations(tmp_path)
    target = project / "pages.pdf"
    _pdf(target, pages=12)

    with pytest.raises(PdfProcessingError, match="outside"):
        _read_pdf(
            operations,
            str(target),
            pages="13",
        )
    with pytest.raises(PdfProcessingError, match="at most 10"):
        _read_pdf(
            operations,
            str(target),
            kind=ReadCursorKind.PDF_RENDER,
            pages="all",
        )
    with pytest.raises(PdfProcessingError, match="DPI"):
        _read_pdf(
            operations,
            str(target),
            kind=ReadCursorKind.PDF_RENDER,
            dpi=400,
        )
