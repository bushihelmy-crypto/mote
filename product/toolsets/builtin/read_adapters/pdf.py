"""PDF view formatting and render-page preparation for Read."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.file import PdfView, PdfViewMode
from mote.product.toolsets.builtin.read_adapters.text import add_line_numbers
from mote.runtime.context.markers import system_reminder

_EMPTY_DOCUMENT = "Warning: the document exists but no text could be extracted."
_PARTIAL = (
    "This is a partial view. Continue with cursor='{cursor}' to read the next "
    "page from the same immutable snapshot (next position: {offset})."
)


@dataclass(frozen=True, slots=True)
class OversizedPdfPage(Exception):
    page_number: int
    size: int


def pdf_view_data(view: PdfView) -> dict:
    return {
        "type": f"pdf_{view.mode.value}",
        "pages": [page.page_number for page in view.pages],
        "total_pages": view.total_pages,
        "status": view.status.value,
        "next_pages": view.next_pages,
        "next_cursor": view.next_cursor,
        "snapshot_digest": view.snapshot.version.digest,
    }


def format_pdf_text(view: PdfView) -> tuple[str, dict]:
    """Format a PDF text view while preserving page boundaries."""
    if view.mode != PdfViewMode.TEXT:
        raise ValueError("format_pdf_text requires a text PDF view")
    sections = [
        f"--- PDF page {page.page_number}/{view.total_pages} ---\n" + add_line_numbers(list(page.lines), 1)
        for page in view.pages
    ]
    output = "\n\n".join(sections) or _EMPTY_DOCUMENT
    if view.next_cursor is not None:
        output += "\n\n" + system_reminder(_PARTIAL.format(cursor=view.next_cursor, offset=view.next_pages))
    return output, pdf_view_data(view)


def prepare_pdf_render(
    view: PdfView,
    *,
    max_page_bytes: int,
) -> tuple[list, list[str], dict]:
    """Validate rendered pages and prepare their descriptions and metadata."""
    if view.mode != PdfViewMode.RENDER:
        raise ValueError("prepare_pdf_render requires a rendered PDF view")
    pages = list(view.pages)
    descriptions: list[str] = []
    for page in pages:
        if len(page.png) > max_page_bytes:
            raise OversizedPdfPage(page.page_number, len(page.png))
        descriptions.append(f"PDF page {page.page_number}/{view.total_pages}: " f"{page.width}x{page.height} PNG")
    if view.next_cursor is not None:
        descriptions.append(_PARTIAL.format(cursor=view.next_cursor, offset=view.next_pages))
    return pages, descriptions, pdf_view_data(view)


__all__ = [
    "OversizedPdfPage",
    "format_pdf_text",
    "pdf_view_data",
    "prepare_pdf_render",
]
