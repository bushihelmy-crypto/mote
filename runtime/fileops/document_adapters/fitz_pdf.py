"""PyMuPDF PDF extraction adapter."""

from __future__ import annotations

import fitz  # type: ignore

from mote.runtime.fileops.document_budgets import BoundedTextSink


def extract(file_path: str, *, sink: BoundedTextSink) -> None:
    with fitz.open(file_path) as document:
        separator = ""
        for page in document:
            sink.write(separator)
            sink.write(page.get_text("text"))  # type: ignore[attr-defined]
            separator = "\n"


def page_count(file_path: str) -> int:
    with fitz.open(file_path) as document:
        return len(document)


def extract_pages(file_path: str, page_numbers: tuple[int, ...]) -> tuple[str, ...]:
    with fitz.open(file_path) as document:
        return tuple(document[page_number - 1].get_text("text") for page_number in page_numbers)


def render_pages(
    file_path: str,
    page_numbers: tuple[int, ...],
    *,
    dpi: int,
) -> tuple[tuple[bytes, int, int], ...]:
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    with fitz.open(file_path) as document:
        rendered = []
        for page_number in page_numbers:
            pixmap = document[page_number - 1].get_pixmap(
                matrix=matrix,
                alpha=False,
            )
            rendered.append((pixmap.tobytes("png"), pixmap.width, pixmap.height))
        return tuple(rendered)
