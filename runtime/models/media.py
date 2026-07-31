"""Media decoding and provider wire-format helpers."""

from __future__ import annotations

import base64
import binascii
import re
from io import BytesIO
from typing import Optional

import fitz
import requests
from PIL import Image

from mote.runtime.telemetry.logging import logger


def decode_image(value: str) -> Image.Image:
    if value.startswith("http"):
        response = requests.get(value)
        response.raise_for_status()
        return Image.open(BytesIO(response.content))
    payload = re.sub(r"^data:image/.+;base64,", "", value)
    return Image.open(BytesIO(base64.b64decode(payload)))


def sniff_image_media_type(payload: str) -> str | None:
    try:
        header = base64.b64decode(payload[:64], validate=False)
    except (binascii.Error, ValueError):
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_image_media_type(payload: str, declared: Optional[str] = None) -> str:
    return sniff_image_media_type(payload) or declared or "image/jpeg"


def build_data_url(payload: str, declared: Optional[str] = None) -> str:
    return f"data:{resolve_image_media_type(payload, declared)};base64,{payload}"


def parse_data_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, separator, payload = url.partition(",")
    if not separator:
        return None
    return header[len("data:") :].split(";", 1)[0].strip(), payload


def pdfs_within_limits(
    pdfs: list[str], max_total_pdf_bytes: int = 15 * 1024 * 1024, max_total_pdf_pages: int = 80
) -> tuple[bool, int, int]:
    total_bytes = total_pages = 0
    for raw in filter(None, pdfs):
        payload = re.sub(r"^data:application/pdf;base64,", "", raw)
        try:
            decoded = base64.b64decode(payload)
        except Exception as exc:
            logger.warning(f"Decode base64 PDF failed, using length estimate: {exc}")
            total_bytes += int(len(payload) * 3 / 4)
            continue
        total_bytes += len(decoded)
        try:
            with fitz.open(stream=decoded, filetype="pdf") as document:
                total_pages += document.page_count
        except Exception as exc:
            logger.warning(f"PyMuPDF failed to read pages: {exc}")
    return total_bytes <= max_total_pdf_bytes and total_pages <= max_total_pdf_pages, total_bytes, total_pages


__all__ = [
    "build_data_url",
    "decode_image",
    "parse_data_url",
    "pdfs_within_limits",
    "resolve_image_media_type",
    "sniff_image_media_type",
]
