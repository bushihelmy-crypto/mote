"""Deterministic search over sealed file snapshots."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from typing import Optional

from mote.contracts.fileops.errors import (
    ContentChangedError,
    DocumentExtractionError,
    DocumentExtractorUnavailableError,
    DocumentResourceLimitError,
    EncodingRejectedError,
    FileBinaryContentError,
    FileOperationError,
    IdentityChangedError,
    ReadCursorError,
    SearchCursorError,
    SearchPatternError,
    SnapshotDurabilityError,
)
from mote.contracts.fileops.models import (
    BlobRef,
    PathToken,
    PresentVersion,
    SearchOutputMode,
    SearchResult,
    SearchRow,
    SearchSkippedFile,
    SearchSkipReason,
    SearchStatus,
    SearchSummary,
)
from mote.contracts.fileops.serialization import (
    blob_from_dict,
    blob_to_dict,
    search_row_from_dict,
    search_row_to_dict,
    search_skipped_from_dict,
    search_skipped_to_dict,
    search_summary_from_dict,
    search_summary_to_dict,
)
from mote.runtime.fileops.artifact_budgets import (
    ARTIFACT_WRITE_TTL_SECONDS,
    MAX_SEARCH_MANIFEST_BYTES,
    MAX_SEARCH_RESULT_BYTES,
    snapshot_budget,
)
from mote.runtime.fileops.artifact_owners import artifact_owner
from mote.runtime.fileops.artifact_repository import ArtifactRepository, ArtifactWriteScope
from mote.runtime.fileops.candidate_discovery import CandidateDiscoveryService
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.query_semantics import CandidateDiscoveryRequest, RegexProgram, RegexProgramError
from mote.runtime.fileops.text_layout import line_number_at, text_layout
from mote.runtime.fileops.text_sources import MaterializedText, TextSourceService

_RESULT_FORMAT = 2
_RESULT_KEYS = frozenset(
    {
        "format_version",
        "rows_artifact",
        "row_count",
        "skipped_artifact",
        "summary",
        "skipped_preview",
        "output_mode",
        "content_search",
    }
)
_DEFAULT_PAGE_SIZE = 1_000
_SKIPPED_PREVIEW_SIZE = 100


class SearchEngine:
    """Discovers candidates once and matches one immutable version per file."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        sources: TextSourceService,
        discovery: CandidateDiscoveryService,
        cursors: DurableCursorRegistry,
    ) -> None:
        self.artifacts = artifacts
        self.sources = sources
        self.discovery = discovery
        self.cursors = cursors

    def search(
        self,
        *,
        root: str,
        content: str = "",
        files: str = "",
        type_name: str = "",
        output_mode: SearchOutputMode = SearchOutputMode.FILES_WITH_MATCHES,
        case_insensitive: bool = False,
        before_context: int = 0,
        after_context: int = 0,
        multiline: bool = False,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        cursor: Optional[str] = None,
        timeout: float = 20.0,
        expected_epoch: int,
        scope: ArtifactWriteScope | None = None,
    ) -> SearchResult:
        if offset < 0 or (limit is not None and limit < 0):
            raise SearchCursorError("search offset and limit must be non-negative")
        if cursor:
            if scope is not None:
                raise SearchCursorError("search continuation cannot create artifacts")
            if offset:
                raise SearchCursorError("offset cannot be combined with a search cursor")
            try:
                opened = self.cursors.open(cursor, expected_namespace="search")
            except ReadCursorError as exc:
                raise SearchCursorError("invalid search cursor", cause=exc) from exc
            return self._page(
                opened.lease.root_manifest,
                opened.position,
                limit,
                expected_epoch=expected_epoch,
                cursor_token=cursor,
            )
        if scope is None:
            raise SearchCursorError("initial search requires an artifact scope")
        try:
            regex = (
                RegexProgram.for_search(
                    content,
                    case_insensitive=case_insensitive,
                    dot_matches_newline=multiline,
                )
                if content
                else None
            )
        except RegexProgramError as exc:
            raise SearchPatternError(
                f"invalid regular expression: {exc}",
                pattern=content,
                cause=exc,
            ) from exc

        started = time.monotonic()
        discovery = self.discovery.discover(
            CandidateDiscoveryRequest(
                root=path_token(root),
                globs=self._split_globs(files),
                type_name=type_name,
            ),
            timeout=timeout,
        )
        candidates = iter(discovery.candidates)
        skipped_preview: list[SearchSkippedFile] = []
        skipped_files = 0
        discovered_files = 0
        total_occurrences = 0
        scanned_files = 0
        matched_files = 0
        row_count = 0
        scan_complete = True
        termination = ""
        deadline = started + timeout
        result_bytes = 0

        def row_chunks() -> Iterable[bytes]:
            nonlocal matched_files
            nonlocal discovered_files
            nonlocal row_count
            nonlocal scan_complete
            nonlocal scanned_files
            nonlocal skipped_files
            nonlocal termination
            nonlocal total_occurrences
            nonlocal result_bytes
            for candidate in candidates:
                discovered_files += 1
                if time.monotonic() >= deadline:
                    scan_complete = False
                    termination = "timeout"
                    discovered_files += sum(1 for _ in candidates)
                    break
                scanned_files += 1
                if regex is None:
                    matched_files += 1
                    row = SearchRow(
                        path=candidate,
                        version=None,
                        line_number=None,
                        text="",
                        matched_text="",
                        occurrence_count=0,
                    )
                    row_count += 1
                    raw_row = self._row_bytes(row)
                    result_bytes += len(raw_row)
                    self._require_result_budget(result_bytes)
                    yield raw_row
                    continue
                source = self._searchable_text(
                    candidate,
                    encoding=encoding,
                    fallback_encoding=fallback_encoding,
                )
                if isinstance(source, SearchSkippedFile):
                    skipped_files += 1
                    if len(skipped_preview) < _SKIPPED_PREVIEW_SIZE:
                        skipped_preview.append(source)
                    raw_skipped = self._skipped_bytes(source)
                    result_bytes += len(raw_skipped)
                    self._require_result_budget(result_bytes)
                    skipped_stream.write(raw_skipped)
                    continue
                occurrences, file_rows = self._rows_for_file(
                    candidate,
                    source.snapshot.version,
                    source.text,
                    regex,
                    output_mode,
                    before_context,
                    after_context,
                    multiline,
                )
                if not occurrences:
                    continue
                matched_files += 1
                total_occurrences += occurrences
                for row in file_rows:
                    row_count += 1
                    raw_row = self._row_bytes(row)
                    result_bytes += len(raw_row)
                    self._require_result_budget(result_bytes)
                    yield raw_row

        with tempfile.TemporaryFile() as skipped_stream:
            rows_artifact = scope.put_chunks(
                row_chunks(),
                maximum_bytes=MAX_SEARCH_RESULT_BYTES,
            )
            skipped_stream.seek(0)
            skipped_artifact = scope.put_chunks(
                iter(lambda: skipped_stream.read(64 * 1_024), b""),
                maximum_bytes=MAX_SEARCH_RESULT_BYTES - rows_artifact.size,
            )

        summary = SearchSummary(
            discovered_files=discovered_files,
            scanned_files=scanned_files,
            matched_files=matched_files,
            total_occurrences=total_occurrences,
            skipped_files=skipped_files,
            complete=scan_complete,
            termination=termination,
        )
        artifact = self._persist(
            scope,
            rows_artifact,
            row_count,
            skipped_artifact,
            summary,
            skipped_preview,
            output_mode=output_mode,
            content_search=regex is not None,
        )
        result = self._page(
            artifact,
            offset,
            limit,
            expected_epoch=expected_epoch,
            cursor_token=None,
        )
        if result.next_cursor is None:
            scope.discard()
        else:
            scope.complete(durability_root=self.cursors.path.parent)
        return result

    def _searchable_text(
        self,
        candidate: PathToken,
        *,
        encoding: Optional[str],
        fallback_encoding: Optional[str],
    ) -> MaterializedText | SearchSkippedFile:
        try:
            source_bytes = os.stat(candidate.native).st_size
            with self.artifacts.write_scope(
                owner=artifact_owner("search-source", candidate.native),
                maximum_bytes=snapshot_budget(source_bytes),
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            ) as scope:
                materialized = self.sources.materialize(
                    candidate,
                    scope=scope,
                    encoding=encoding,
                    fallback_encoding=fallback_encoding,
                )
                scope.discard()
                return materialized
        except FileBinaryContentError as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.BINARY,
                detail=str(exc),
            )
        except DocumentExtractorUnavailableError as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.EXTRACTOR_UNAVAILABLE,
                detail=str(exc),
            )
        except DocumentResourceLimitError as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.RESOURCE_LIMIT,
                detail=str(exc),
            )
        except DocumentExtractionError as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.EXTRACTION,
                detail=str(exc),
            )
        except EncodingRejectedError as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.ENCODING,
                detail=str(exc),
            )
        except (ContentChangedError, IdentityChangedError) as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.CHANGED,
                detail=str(exc),
            )
        except (OSError, FileOperationError) as exc:
            return SearchSkippedFile(
                path=candidate,
                reason=SearchSkipReason.IO,
                detail=str(exc),
            )

    @staticmethod
    def _rows_for_file(
        path: PathToken,
        version: PresentVersion,
        text: str,
        regex: RegexProgram,
        output_mode: SearchOutputMode,
        before_context: int,
        after_context: int,
        multiline: bool,
    ) -> tuple[int, Iterable[SearchRow]]:
        occurrence_count = regex.occurrence_count(text)
        if not occurrence_count:
            return 0, ()
        line_starts, lines = text_layout(text)

        def line_number(match: re.Match[str]) -> int:
            return line_number_at(line_starts, match.start())

        if output_mode == SearchOutputMode.FILES_WITH_MATCHES:
            first = next(regex.finditer(text), None)
            if first is None:
                return 0, ()
            return occurrence_count, (
                SearchRow(
                    path=path,
                    version=version,
                    line_number=line_number(first),
                    text="",
                    matched_text=first.group(0),
                    occurrence_count=occurrence_count,
                ),
            )
        if output_mode == SearchOutputMode.COUNT:
            return occurrence_count, (
                SearchRow(
                    path=path,
                    version=version,
                    line_number=None,
                    text="",
                    matched_text="",
                    occurrence_count=occurrence_count,
                ),
            )
        if output_mode == SearchOutputMode.ONLY_MATCHING or multiline:
            return occurrence_count, (
                SearchRow(
                    path=path,
                    version=version,
                    line_number=line_number(match),
                    text=match.group(0),
                    matched_text=match.group(0),
                    occurrence_count=1,
                )
                for match in regex.finditer(text)
            )

        matches_by_line: dict[int, tuple[str, int]] = {}
        for match in regex.finditer(text):
            number = line_number(match)
            first_text, count = matches_by_line.get(number, (match.group(0), 0))
            matches_by_line[number] = (first_text, count + 1)
        included: set[int] = set()
        for number in matches_by_line:
            start = max(1, number - before_context)
            end = min(len(lines), number + after_context)
            included.update(range(start, end + 1))
        return occurrence_count, tuple(
            SearchRow(
                path=path,
                version=version,
                line_number=number,
                text=lines[number - 1],
                matched_text=(matches_by_line[number][0] if number in matches_by_line else ""),
                occurrence_count=matches_by_line.get(number, ("", 0))[1],
                is_context=number not in matches_by_line,
            )
            for number in sorted(included)
        )

    def _persist(
        self,
        scope: ArtifactWriteScope,
        rows_artifact: BlobRef,
        row_count: int,
        skipped_artifact: BlobRef,
        summary: SearchSummary,
        skipped_preview: list[SearchSkippedFile],
        *,
        output_mode: SearchOutputMode,
        content_search: bool,
    ) -> BlobRef:
        payload = {
            "format_version": _RESULT_FORMAT,
            "rows_artifact": blob_to_dict(rows_artifact),
            "row_count": row_count,
            "skipped_artifact": blob_to_dict(skipped_artifact),
            "summary": search_summary_to_dict(summary),
            "skipped_preview": [search_skipped_to_dict(item) for item in skipped_preview],
            "output_mode": output_mode.value,
            "content_search": content_search,
        }
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > MAX_SEARCH_MANIFEST_BYTES:
            raise SearchCursorError(
                "search result manifest exceeds the size limit",
                size=len(raw),
                maximum=MAX_SEARCH_MANIFEST_BYTES,
            )
        return scope.put_bytes(raw)

    @staticmethod
    def _row_bytes(row: SearchRow) -> bytes:
        return (
            json.dumps(
                search_row_to_dict(row),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _skipped_bytes(skipped: SearchSkippedFile) -> bytes:
        return (
            json.dumps(
                search_skipped_to_dict(skipped),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def _page(
        self,
        artifact: BlobRef,
        offset: int,
        limit: Optional[int],
        *,
        expected_epoch: int,
        cursor_token: str | None,
    ) -> SearchResult:
        try:
            payload = json.loads(
                self.artifacts.read_bounded(
                    artifact,
                    maximum_bytes=MAX_SEARCH_MANIFEST_BYTES,
                ).decode("utf-8")
            )
            if type(payload) is not dict or set(payload) != _RESULT_KEYS:
                raise ValueError("search result fields are not canonical")
            if type(payload["format_version"]) is not int:
                raise TypeError("search result format is not an integer")
            if payload["format_version"] != _RESULT_FORMAT:
                raise ValueError("unsupported search result format")
            rows_artifact = blob_from_dict(payload["rows_artifact"])
            if rows_artifact is None:
                raise ValueError("search rows artifact is missing")
            skipped_artifact = blob_from_dict(payload["skipped_artifact"])
            if skipped_artifact is None:
                raise ValueError("search skipped artifact is missing")
            row_count = payload["row_count"]
            if type(row_count) is not int or row_count < 0:
                raise ValueError("negative search row count")
            summary = search_summary_from_dict(payload["summary"])
            if type(payload["skipped_preview"]) is not list:
                raise TypeError("search skipped preview is not a list")
            skipped = tuple(search_skipped_from_dict(item) for item in payload["skipped_preview"])
            if type(payload["output_mode"]) is not str:
                raise TypeError("search output mode is not a string")
            output_mode = SearchOutputMode(payload["output_mode"])
            if type(payload["content_search"]) is not bool:
                raise TypeError("search content flag is not a boolean")
            content_search = payload["content_search"]
            if offset > row_count:
                raise ValueError("search cursor position exceeds the result")
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
            SnapshotDurabilityError,
        ) as exc:
            raise SearchCursorError("search result artifact is invalid", cause=exc) from exc
        page_limit = limit or _DEFAULT_PAGE_SIZE
        page_rows: list[SearchRow] = []
        try:
            for index, raw_row in enumerate(self.artifacts.iter_lines(rows_artifact)):
                if index < offset:
                    continue
                if len(page_rows) >= page_limit:
                    break
                page_rows.append(search_row_from_dict(json.loads(raw_row)))
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
            SnapshotDurabilityError,
        ) as exc:
            raise SearchCursorError("search rows artifact is invalid", cause=exc) from exc
        rows = tuple(page_rows)
        end = min(row_count, offset + len(rows))
        surfaced: set[str | bytes] = set()
        files_list: list[PathToken] = []
        for row in rows:
            if row.path.native in surfaced:
                continue
            surfaced.add(row.path.native)
            files_list.append(row.path)
        files = tuple(files_list)
        next_cursor = (
            (
                self.cursors.advance(
                    cursor_token,
                    expected_namespace="search",
                    position=end,
                )
                if cursor_token is not None
                else self.cursors.issue(
                    namespace="search",
                    root_manifest=artifact,
                    pinned_artifacts=(rows_artifact, skipped_artifact),
                    position=end,
                    expected_epoch=expected_epoch,
                )
            )
            if end < row_count
            else None
        )
        status = SearchStatus.COMPLETE if summary.complete and next_cursor is None else SearchStatus.PARTIAL
        return SearchResult(
            rows=rows,
            files=files,
            summary=summary,
            skipped=skipped,
            artifact=artifact,
            skipped_artifact=skipped_artifact,
            skipped_truncated=summary.skipped_files > len(skipped),
            output_mode=output_mode,
            content_search=content_search,
            status=status,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _split_globs(value: str) -> tuple[str, ...]:
        patterns: list[str] = []
        for raw in value.split():
            if "{" in raw and "}" in raw:
                patterns.append(raw)
            else:
                patterns.extend(item for item in raw.split(",") if item)
        return tuple(patterns)

    @staticmethod
    def _require_result_budget(result_bytes: int) -> None:
        if result_bytes > MAX_SEARCH_RESULT_BYTES:
            raise SearchCursorError(
                "search result exceeds the durable result budget",
                size=result_bytes,
                maximum=MAX_SEARCH_RESULT_BYTES,
            )


__all__ = ["SearchEngine"]
