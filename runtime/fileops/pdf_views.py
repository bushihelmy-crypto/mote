"""Page-aware PDF text and image views over sealed artifacts."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Optional

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.codec import snapshot_from_dict, snapshot_to_dict
from mote.contracts.file.errors import PdfProcessingError, ReadCursorError
from mote.contracts.file.identity import FileSnapshot
from mote.contracts.file.views import PdfPageView, PdfView, PdfViewMode, ReadCursorKind, ReadViewStatus
from mote.runtime.fileops.capture import ManagedSnapshotCapture
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, FileMutationArtifactRepository
from mote.runtime.fileops.read_cursors import OpenReadCursor, ReadCursorStore
from mote.runtime.fileops.text_layout import text_layout

try:
    from mote.runtime.fileops.document_adapters.fitz_pdf import extract_pages as _fitz_extract_pages
    from mote.runtime.fileops.document_adapters.fitz_pdf import page_count as _fitz_page_count
    from mote.runtime.fileops.document_adapters.fitz_pdf import render_pages as _fitz_render_pages
except ImportError:
    _fitz_extract_pages = None
    _fitz_page_count = None
    _fitz_render_pages = None

try:
    from mote.runtime.fileops.document_adapters.pypdf_pdf import extract_pages as _pypdf_extract_pages
    from mote.runtime.fileops.document_adapters.pypdf_pdf import page_count as _pypdf_page_count
except ImportError:
    _pypdf_extract_pages = None
    _pypdf_page_count = None


_MAX_PDF_BYTES = 10 * 1_024 * 1_024
_DEFAULT_TEXT_PAGES = 20
_DEFAULT_RENDER_PAGES = 1
_MAX_TEXT_PAGES = 100
_MAX_RENDER_PAGES = 10
_MIN_DPI = 72
_MAX_DPI = 300


class PdfViewService:
    """Selects and extracts or renders explicit pages from one PDF version."""

    def __init__(
        self,
        *,
        artifacts: FileMutationArtifactRepository,
        capture: ManagedSnapshotCapture,
        cursors: ReadCursorStore,
    ) -> None:
        self.artifacts = artifacts
        self.capture = capture
        self.cursors = cursors

    def read(
        self,
        path: str,
        *,
        mode: PdfViewMode,
        pages: str = "",
        dpi: int = 144,
        limit: Optional[int] = None,
        continuation: Optional[OpenReadCursor] = None,
        scope: ArtifactWriteScope | None = None,
        expected_epoch: int,
    ) -> PdfView:
        if mode == PdfViewMode.RENDER and not _MIN_DPI <= dpi <= _MAX_DPI:
            raise PdfProcessingError(
                f"PDF render DPI must be between {_MIN_DPI} and {_MAX_DPI}",
                dpi=dpi,
            )
        if limit is not None and limit <= 0:
            raise PdfProcessingError(
                "PDF continuation limit must be positive",
                limit=limit,
            )
        maximum = _MAX_TEXT_PAGES if mode == PdfViewMode.TEXT else _MAX_RENDER_PAGES
        if limit is not None and limit > maximum:
            raise PdfProcessingError(
                f"PDF {mode.value} view accepts at most {maximum} pages per call",
                limit=limit,
                maximum=maximum,
            )
        manifest = None
        continuation_start = None
        adapter_id = None
        if continuation is None:
            if scope is None:
                raise ReadCursorError("initial PDF read requires an artifact scope")
            snapshot = self.capture.capture(path, scope=scope)
        else:
            if scope is not None:
                raise ReadCursorError("PDF continuation cannot create artifacts")
            if pages.strip():
                raise ReadCursorError("PDF cursor cannot be combined with explicit pages")
            snapshot, manifest, continuation_start, dpi, adapter_id = self._resume(
                path,
                mode,
                continuation,
            )
        if snapshot.version.size > _MAX_PDF_BYTES:
            raise PdfProcessingError(
                f"PDF exceeds the {_MAX_PDF_BYTES}-byte processing limit",
                size=snapshot.version.size,
                maximum=_MAX_PDF_BYTES,
            )
        raw = self.artifacts.read_bytes(snapshot.artifact)
        with tempfile.TemporaryDirectory(prefix="mote-pdf-") as directory:
            artifact = Path(directory) / "snapshot.pdf"
            artifact.write_bytes(raw)
            try:
                adapter_id, count_pages, extract_pages = self._text_adapter(
                    adapter_id or ("fitz" if mode == PdfViewMode.RENDER else None)
                )
                total_pages = count_pages(str(artifact))
                if continuation_start is None:
                    selected, defaulted = self._select_pages(
                        pages,
                        total_pages=total_pages,
                        mode=mode,
                        limit=limit,
                    )
                else:
                    selected = self._default_pages(
                        continuation_start,
                        total_pages=total_pages,
                        mode=mode,
                        limit=limit,
                    )
                    defaulted = True
                if mode == PdfViewMode.TEXT:
                    extracted = extract_pages(str(artifact), selected)
                    page_views = tuple(
                        PdfPageView(
                            page_number=number,
                            text=text,
                            lines=text_layout(text)[1],
                        )
                        for number, text in zip(selected, extracted, strict=True)
                    )
                else:
                    if _fitz_render_pages is None:
                        raise PdfProcessingError("PDF page rendering requires PyMuPDF")
                    rendered = _fitz_render_pages(
                        str(artifact),
                        selected,
                        dpi=dpi,
                    )
                    page_views = tuple(
                        PdfPageView(
                            page_number=number,
                            png=png,
                            width=width,
                            height=height,
                        )
                        for number, (png, width, height) in zip(
                            selected,
                            rendered,
                            strict=True,
                        )
                    )
            except PdfProcessingError:
                raise
            except Exception as exc:
                raise PdfProcessingError(
                    f"cannot process PDF pages: {exc}",
                    path=path,
                    cause=exc,
                ) from exc

        next_pages = None
        next_cursor = None
        if defaulted and selected and selected[-1] < total_pages:
            next_start = selected[-1] + 1
            page_count = limit or (_DEFAULT_TEXT_PAGES if mode == PdfViewMode.TEXT else _DEFAULT_RENDER_PAGES)
            next_end = min(total_pages, next_start + page_count - 1)
            next_pages = str(next_start) if next_start == next_end else f"{next_start}-{next_end}"
            if manifest is None:
                if scope is None:
                    raise ReadCursorError("PDF cursor manifest has no artifact scope")
                manifest = self.cursors.persist(
                    scope,
                    self._kind(mode),
                    {
                        "snapshot": snapshot_to_dict(snapshot),
                        "dpi": dpi,
                        "adapter": adapter_id,
                    },
                )
            next_cursor = (
                self.cursors.advance(continuation, next_start)
                if continuation is not None
                else self.cursors.issue(
                    manifest,
                    next_start,
                    expected_epoch=expected_epoch,
                )
            )
        self.cursors.observe(snapshot, expected_epoch=expected_epoch)
        if scope is not None:
            scope.complete(durability_root=self.cursors.registry.path.parent)
        return PdfView(
            snapshot=snapshot,
            mode=mode,
            status=(ReadViewStatus.PARTIAL if next_cursor is not None else ReadViewStatus.COMPLETE),
            total_pages=total_pages,
            pages=page_views,
            next_pages=next_pages,
            next_cursor=next_cursor,
        )

    def _resume(
        self,
        path: str,
        mode: PdfViewMode,
        opened: OpenReadCursor,
    ) -> tuple[FileSnapshot, ContentIdentity, int, int, str]:
        try:
            if opened.kind != self._kind(mode):
                raise ValueError("cursor PDF mode does not match the request")
            snapshot = snapshot_from_dict(opened.payload["snapshot"])
            dpi = opened.payload["dpi"]
            adapter = opened.payload["adapter"]
            if type(dpi) is not int:
                raise TypeError("cursor PDF DPI is not an integer")
            if type(adapter) is not str or not adapter:
                raise TypeError("cursor PDF adapter is invalid")
            if path_token(path).native != snapshot.requested_path.native:
                raise ValueError("cursor belongs to a different file")
            if opened.position < 1:
                raise ValueError("cursor PDF page is invalid")
            if mode == PdfViewMode.RENDER and not _MIN_DPI <= dpi <= _MAX_DPI:
                raise ValueError("cursor PDF render DPI is invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadCursorError("PDF cursor manifest is invalid", cause=exc) from exc
        return snapshot, opened.manifest, opened.position, dpi, adapter

    @staticmethod
    def _kind(mode: PdfViewMode) -> ReadCursorKind:
        return ReadCursorKind.PDF_TEXT if mode == PdfViewMode.TEXT else ReadCursorKind.PDF_RENDER

    @staticmethod
    def _default_pages(
        start: int,
        *,
        total_pages: int,
        mode: PdfViewMode,
        limit: Optional[int],
    ) -> tuple[int, ...]:
        if start > total_pages:
            raise ReadCursorError(
                "PDF cursor page exceeds the snapshot",
                page=start,
                total_pages=total_pages,
            )
        count = limit or (_DEFAULT_TEXT_PAGES if mode == PdfViewMode.TEXT else _DEFAULT_RENDER_PAGES)
        return tuple(range(start, min(total_pages, start + count - 1) + 1))

    @staticmethod
    def _text_adapter(
        required: Optional[str] = None,
    ) -> tuple[
        str,
        Callable[[str], int],
        Callable[[str, tuple[int, ...]], tuple[str, ...]],
    ]:
        if required in (None, "fitz") and (_fitz_page_count is not None and _fitz_extract_pages is not None):
            return "fitz", _fitz_page_count, _fitz_extract_pages
        if required in (None, "pypdf") and (_pypdf_page_count is not None and _pypdf_extract_pages is not None):
            return "pypdf", _pypdf_page_count, _pypdf_extract_pages
        if required is not None:
            raise PdfProcessingError(
                f"PDF cursor requires unavailable adapter '{required}'",
                adapter=required,
            )
        raise PdfProcessingError("PDF page extraction requires PyMuPDF, pypdf, or PyPDF2")

    @staticmethod
    def _select_pages(
        spec: str,
        *,
        total_pages: int,
        mode: PdfViewMode,
        limit: Optional[int] = None,
    ) -> tuple[tuple[int, ...], bool]:
        if total_pages <= 0:
            return (), not spec.strip()
        normalized = spec.strip().lower()
        defaulted = not normalized
        if defaulted:
            count = min(
                total_pages,
                limit or (_DEFAULT_TEXT_PAGES if mode == PdfViewMode.TEXT else _DEFAULT_RENDER_PAGES),
            )
            selected = tuple(range(1, count + 1))
        elif normalized == "all":
            selected = tuple(range(1, total_pages + 1))
        else:
            ordered: list[int] = []
            seen: set[int] = set()
            try:
                for item in normalized.split(","):
                    token = item.strip()
                    if not token:
                        raise ValueError("empty page token")
                    if "-" in token:
                        start_text, end_text = token.split("-", 1)
                        start = int(start_text)
                        end = int(end_text)
                        if start > end:
                            raise ValueError("descending page range")
                        numbers = range(start, end + 1)
                    else:
                        numbers = (int(token),)
                    for number in numbers:
                        if number < 1 or number > total_pages:
                            raise ValueError(f"page {number} is outside 1-{total_pages}")
                        if number not in seen:
                            seen.add(number)
                            ordered.append(number)
            except ValueError as exc:
                raise PdfProcessingError(
                    f"invalid PDF page selection '{spec}': {exc}",
                    pages=spec,
                    cause=exc,
                ) from exc
            selected = tuple(ordered)

        maximum = _MAX_TEXT_PAGES if mode == PdfViewMode.TEXT else _MAX_RENDER_PAGES
        if len(selected) > maximum:
            raise PdfProcessingError(
                f"PDF {mode.value} view accepts at most {maximum} pages per call",
                selected=len(selected),
                maximum=maximum,
            )
        return selected, defaulted


__all__ = ["PdfViewService"]
