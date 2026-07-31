"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Tuple, Union

from mote.contracts.file.identity import FileSnapshot


class ByteViewMode(StrEnum):
    RAW = "raw"
    HEX = "hex"


class ReadViewStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class PdfViewMode(StrEnum):
    TEXT = "text"
    RENDER = "render"


class TextViewMode(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"


@dataclass(frozen=True)
class ExtractionBudget:
    max_archive_uncompressed_bytes: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("max_archive_uncompressed_bytes", self.max_archive_uncompressed_bytes),
            ("max_output_bytes", self.max_output_bytes),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


class ReadCursorKind(StrEnum):
    TEXT = "text"
    RAW = "raw"
    HEX = "hex"
    PDF_TEXT = "pdf_text"
    PDF_RENDER = "pdf_render"


@dataclass(frozen=True)
class TextReadRequest:
    offset: Optional[int] = None
    limit: Optional[int] = None
    encoding: Optional[str] = None
    fallback_encoding: Optional[str] = None


@dataclass(frozen=True)
class ByteReadRequest:
    mode: ByteViewMode
    offset: Optional[int] = None
    limit: Optional[int] = None


@dataclass(frozen=True)
class PdfReadRequest:
    mode: PdfViewMode
    pages: str = ""
    dpi: int = 144
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        if self.pages.strip() and self.limit is not None:
            raise ValueError("PDF pages cannot be combined with a page limit")


@dataclass(frozen=True)
class ContinueReadRequest:
    cursor: str
    limit: Optional[int] = None


ReadRequest = Union[
    TextReadRequest,
    ByteReadRequest,
    PdfReadRequest,
    ContinueReadRequest,
]


@dataclass(frozen=True)
class FileByteView:
    snapshot: FileSnapshot
    mode: ByteViewMode
    status: ReadViewStatus
    offset: int
    next_offset: Optional[int]
    total_bytes: int
    data: bytes
    text: str = ""
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class PdfPageView:
    page_number: int
    text: str = ""
    lines: Tuple[str, ...] = ()
    png: bytes = b""
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class PdfView:
    snapshot: FileSnapshot
    mode: PdfViewMode
    status: ReadViewStatus
    total_pages: int
    pages: Tuple[PdfPageView, ...]
    next_pages: Optional[str] = None
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class FileTextView:
    snapshot: FileSnapshot
    mode: TextViewMode
    status: ReadViewStatus
    offset: int
    next_offset: Optional[int]
    total_lines: int
    lines: Tuple[str, ...]
    next_cursor: Optional[str] = None
