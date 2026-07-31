"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Tuple

from mote.contracts.file.identity import PathToken, PresentVersion


class SearchOutputMode(StrEnum):
    FILES_WITH_MATCHES = "files_with_matches"
    CONTENT = "content"
    COUNT = "count"
    ONLY_MATCHING = "only_matching"


class SearchSkipReason(StrEnum):
    BINARY = "binary"
    CHANGED = "changed"
    ENCODING = "encoding"
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
    EXTRACTION = "extraction"
    RESOURCE_LIMIT = "resource_limit"
    IO = "io"


class SearchStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True)
class SearchRow:
    path: PathToken
    version: Optional[PresentVersion]
    line_number: Optional[int]
    text: str
    matched_text: str
    occurrence_count: int
    is_context: bool = False


@dataclass(frozen=True)
class SearchSkippedFile:
    path: PathToken
    reason: SearchSkipReason
    detail: str


@dataclass(frozen=True)
class SearchSummary:
    discovered_files: int
    scanned_files: int
    matched_files: int
    total_occurrences: int
    skipped_files: int
    complete: bool = True
    termination: str = ""


@dataclass(frozen=True)
class SearchResult:
    rows: Tuple[SearchRow, ...]
    files: Tuple[PathToken, ...]
    summary: SearchSummary
    skipped: Tuple[SearchSkippedFile, ...]
    artifact: ContentIdentity
    skipped_artifact: ContentIdentity
    skipped_truncated: bool
    output_mode: SearchOutputMode
    content_search: bool
    status: SearchStatus
    next_cursor: Optional[str] = None
