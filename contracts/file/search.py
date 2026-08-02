"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Tuple

from mote.contracts.content.identity import ContentIdentity
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

    def __post_init__(self) -> None:
        if not isinstance(self.path, PathToken):
            raise ValueError("search row path is invalid")
        if self.version is not None and not isinstance(self.version, PresentVersion):
            raise ValueError("search row version is invalid")
        if self.line_number is not None and (type(self.line_number) is not int or self.line_number <= 0):
            raise ValueError("search row line_number is invalid")
        if type(self.text) is not str or type(self.matched_text) is not str:
            raise ValueError("search row text fields are invalid")
        if type(self.occurrence_count) is not int or self.occurrence_count < 0:
            raise ValueError("search row occurrence_count is invalid")
        if type(self.is_context) is not bool:
            raise ValueError("search row is_context is invalid")


@dataclass(frozen=True)
class SearchSkippedFile:
    path: PathToken
    reason: SearchSkipReason
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, PathToken):
            raise ValueError("search skipped path is invalid")
        if not isinstance(self.reason, SearchSkipReason):
            raise ValueError("search skipped reason is invalid")
        if type(self.detail) is not str:
            raise ValueError("search skipped detail is invalid")


@dataclass(frozen=True)
class SearchSummary:
    discovered_files: int
    scanned_files: int
    matched_files: int
    total_occurrences: int
    skipped_files: int
    complete: bool = True
    termination: str = ""

    def __post_init__(self) -> None:
        for name in (
            "discovered_files",
            "scanned_files",
            "matched_files",
            "total_occurrences",
            "skipped_files",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"search summary {name} is invalid")
        if type(self.complete) is not bool or type(self.termination) is not str:
            raise ValueError("search summary completion fields are invalid")
        if self.termination not in {"", "timeout"}:
            raise ValueError("search summary completion fields are invalid")
        if (self.complete and self.termination) or (not self.complete and not self.termination):
            raise ValueError("search summary completion and termination are inconsistent")


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
