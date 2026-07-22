"""PyMuPDF PDF extraction adapter."""
from __future__ import annotations

import fitz  # type: ignore


def extract(file_path: str) -> str | None:
    try:
        parts = []
        with fitz.open(file_path) as document:
            for page in document:
                parts.append(page.get_text("text"))  # type: ignore[attr-defined]
        return "\n".join(parts)
    except Exception:
        return None
