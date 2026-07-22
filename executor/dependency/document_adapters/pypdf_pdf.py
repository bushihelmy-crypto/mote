"""pypdf/PyPDF2 fallback extraction adapter."""
from __future__ import annotations

try:
    from pypdf import PdfReader  # type: ignore
except ImportError:
    from PyPDF2 import PdfReader  # type: ignore


def extract(file_path: str) -> str | None:
    try:
        reader = PdfReader(file_path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None
