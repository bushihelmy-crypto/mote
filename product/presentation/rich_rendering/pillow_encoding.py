"""Pillow implementation for terminal-protocol PNG encoding."""
from __future__ import annotations

from io import BytesIO

from PIL import Image


def png_bytes(path: str) -> bytes | None:
    try:
        with Image.open(path) as image:
            image = image.convert("RGBA")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 — caller degrades to another renderer
        return None


__all__ = ["png_bytes"]
