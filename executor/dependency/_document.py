"""Shared rich-document text extraction (PDF / Word / Excel).

A single source of truth for turning a binary/zipped document into plain text,
used by BOTH the Grep tool (to search inside documents) and the Read tool (to
read them with line offsets). Keeping extraction here guarantees the two tools
agree on line numbering: a position Grep reports as ``report.pdf:42`` is the
same line Read returns for ``offset=42``.

Not a registered tool (underscore-prefixed, like ``_file_base.py``), so the
registry package scan ignores it.

Line model: every extractor returns one flat string with lines joined by
``"\\n"``. Callers split on ``"\\n"`` (see ``document_lines``) so that the i-th
line (1-indexed) is exactly the text between the (i-1)-th and i-th newline —
matching how Grep computes a match's line number.
"""
from __future__ import annotations

import os
from importlib import import_module
from typing import Callable, Optional

from mote.common.const.tools import DOCUMENT_EXTENSIONS

Extractor = Callable[[str], Optional[str]]

_PDF_ADAPTERS = (
    "mote.executor.dependency.document_adapters.fitz_pdf",
    "mote.executor.dependency.document_adapters.pdfminer_pdf",
    "mote.executor.dependency.document_adapters.pypdf_pdf",
)
_DOCX_ADAPTER = "mote.executor.dependency.document_adapters.docx"
_XLSX_ADAPTER = "mote.executor.dependency.document_adapters.xlsx"


def _load_adapter(module_name: str) -> Optional[Extractor]:
    try:
        module = import_module(module_name)
    except ImportError:
        return None
    return module.extract


def is_document(file_path: str) -> bool:
    """True if the path is a rich document handled via text extraction."""
    return file_path.lower().endswith(DOCUMENT_EXTENSIONS)


def document_lines(text: str) -> list[str]:
    """Split extracted text into lines the way both tools agree on.

    Uses ``str.split("\\n")`` (NOT ``splitlines()``) so line indexing matches
    Grep's ``text.count("\\n", 0, match_start) + 1`` position arithmetic exactly.
    """
    return text.split("\n")


def extract_pdf_text(file_path: str) -> Optional[str]:
    """Extract a PDF's text, pages joined by newlines.

    Tries PyMuPDF (fitz) -> pdfminer -> pypdf/PyPDF2, using whichever is
    installed. Returns the full text or None if no backend is available / the
    extraction fails.
    """
    for module_name in _PDF_ADAPTERS:
        extractor = _load_adapter(module_name)
        if extractor is not None:
            return extractor(file_path)
    return None


def extract_docx_text(file_path: str) -> Optional[str]:
    """Extract text from a Word .docx (paragraphs + table cells) via python-docx."""
    extractor = _load_adapter(_DOCX_ADAPTER)
    return extractor(file_path) if extractor is not None else None


def extract_xlsx_text(file_path: str) -> Optional[str]:
    """Extract text from an Excel .xlsx via openpyxl.

    Emits one line per row as tab-joined cells, prefixed with the sheet name so
    matches can be located. read_only + data_only keeps memory bounded on large
    workbooks.
    """
    extractor = _load_adapter(_XLSX_ADAPTER)
    return extractor(file_path) if extractor is not None else None


def extract_document_text(file_path: str) -> Optional[str]:
    """Dispatch to the right extractor by extension. None = cannot extract."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_text(file_path)
    if ext == ".docx":
        return extract_docx_text(file_path)
    if ext == ".xlsx":
        return extract_xlsx_text(file_path)
    return None
