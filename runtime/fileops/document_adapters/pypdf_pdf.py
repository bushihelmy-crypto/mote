"""pypdf/PyPDF2 fallback extraction adapter."""

from __future__ import annotations

try:
    from pypdf import PdfReader  # type: ignore
except ImportError:
    from PyPDF2 import PdfReader  # type: ignore

from mote.runtime.fileops.document_budgets import BoundedTextSink


def extract(file_path: str, *, sink: BoundedTextSink) -> None:
    reader = PdfReader(file_path)
    separator = ""
    for page in reader.pages:
        sink.write(separator)
        sink.write(page.extract_text() or "")
        separator = "\n"


def page_count(file_path: str) -> int:
    return len(PdfReader(file_path).pages)


def extract_pages(file_path: str, page_numbers: tuple[int, ...]) -> tuple[str, ...]:
    reader = PdfReader(file_path)
    return tuple(reader.pages[page_number - 1].extract_text() or "" for page_number in page_numbers)
