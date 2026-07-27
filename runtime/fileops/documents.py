"""Rich-document extraction over immutable snapshot bytes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from mote.contracts.fileops.errors import (
    DocumentExtractionError,
    DocumentExtractorUnavailableError,
    DocumentResourceLimitError,
)
from mote.contracts.fileops.models import ExtractionBudget
from mote.runtime.fileops.document_budgets import BoundedTextSink, enforce_archive_budget

try:
    from mote.runtime.fileops.document_adapters.docx import extract as _docx_extract
except ImportError:
    _docx_extract = None

try:
    from mote.runtime.fileops.document_adapters.fitz_pdf import extract as _fitz_pdf_extract
except ImportError:
    _fitz_pdf_extract = None

try:
    from mote.runtime.fileops.document_adapters.pdfminer_pdf import extract as _pdfminer_extract
except ImportError:
    _pdfminer_extract = None

try:
    from mote.runtime.fileops.document_adapters.pypdf_pdf import extract as _pypdf_extract
except ImportError:
    _pypdf_extract = None

try:
    from mote.runtime.fileops.document_adapters.xlsx import extract as _xlsx_extract
except ImportError:
    _xlsx_extract = None


class Extractor(Protocol):
    def __call__(
        self,
        file_path: str,
        *,
        sink: BoundedTextSink,
    ) -> None:
        ...


_DOCUMENT_SUFFIXES = (".pdf", ".docx", ".xlsx")

_PDF_ADAPTERS: tuple[Extractor, ...] = tuple(
    extractor for extractor in (_fitz_pdf_extract, _pdfminer_extract, _pypdf_extract) if extractor is not None
)


def is_document(file_path: str) -> bool:
    return file_path.lower().endswith(_DOCUMENT_SUFFIXES)


def extract_pdf_text(file_path: str, *, budget: ExtractionBudget) -> str:
    if not _PDF_ADAPTERS:
        raise DocumentExtractorUnavailableError(
            "no PDF text extractor is installed",
            path=file_path,
        )
    failures: list[str] = []
    for extractor in _PDF_ADAPTERS:
        sink = BoundedTextSink(budget)
        try:
            extractor(file_path, sink=sink)
            return sink.text
        except DocumentResourceLimitError:
            raise
        except Exception as exc:
            failures.append(f"{extractor.__module__}: {exc}")
    raise DocumentExtractionError(
        "all PDF text extractors failed: " + "; ".join(failures),
        path=file_path,
    )


def extract_docx_text(file_path: str, *, budget: ExtractionBudget) -> str:
    if _docx_extract is None:
        raise DocumentExtractorUnavailableError(
            "the DOCX text extractor is not installed",
            path=file_path,
        )
    try:
        enforce_archive_budget(file_path, budget)
        sink = BoundedTextSink(budget)
        _docx_extract(file_path, sink=sink)
        return sink.text
    except DocumentResourceLimitError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(
            f"the DOCX text extractor failed: {exc}",
            path=file_path,
            cause=exc,
        ) from exc


def extract_xlsx_text(file_path: str, *, budget: ExtractionBudget) -> str:
    if _xlsx_extract is None:
        raise DocumentExtractorUnavailableError(
            "the XLSX text extractor is not installed",
            path=file_path,
        )
    try:
        enforce_archive_budget(file_path, budget)
        sink = BoundedTextSink(budget)
        _xlsx_extract(file_path, sink=sink)
        return sink.text
    except DocumentResourceLimitError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(
            f"the XLSX text extractor failed: {exc}",
            path=file_path,
            cause=exc,
        ) from exc


def extract_document_text(file_path: str, *, budget: ExtractionBudget) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_text(file_path, budget=budget)
    if ext == ".docx":
        return extract_docx_text(file_path, budget=budget)
    if ext == ".xlsx":
        return extract_xlsx_text(file_path, budget=budget)
    raise DocumentExtractorUnavailableError(
        "no text extractor exists for this document type",
        path=file_path,
    )


def extract_document_bytes(
    raw: bytes,
    suffix: str,
    *,
    budget: ExtractionBudget,
) -> str:
    normalized = suffix.lower()
    if normalized not in _DOCUMENT_SUFFIXES:
        raise DocumentExtractorUnavailableError(
            f"no text extractor exists for document suffix '{suffix}'",
            suffix=suffix,
        )
    with tempfile.TemporaryDirectory(prefix="mote-document-") as directory:
        artifact = Path(directory) / f"snapshot{normalized}"
        artifact.write_bytes(raw)
        return extract_document_text(str(artifact), budget=budget)


__all__ = [
    "extract_document_bytes",
    "extract_document_text",
    "extract_docx_text",
    "extract_pdf_text",
    "extract_xlsx_text",
    "ExtractionBudget",
    "is_document",
]
