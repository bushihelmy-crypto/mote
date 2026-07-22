"""pdfminer.six PDF extraction adapter."""
from __future__ import annotations

from pdfminer.high_level import extract_text  # type: ignore


def extract(file_path: str) -> str | None:
    try:
        return extract_text(file_path)
    except Exception:
        return None
