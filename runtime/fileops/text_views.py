"""Bounded, immutable pages over the shared text-materialization path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mote.contracts.fileops.errors import (
    EncodingRejectedError,
    FileReadRangeError,
    ReadCursorError,
    SnapshotDurabilityError,
)
from mote.contracts.fileops.models import BlobRef, FileTextView, ReadCursorKind, ReadViewStatus, TextViewMode
from mote.contracts.fileops.serialization import blob_from_dict, blob_to_dict, snapshot_from_dict, snapshot_to_dict
from mote.runtime.fileops.artifact_budgets import MAX_MATERIALIZED_TEXT_BYTES
from mote.runtime.fileops.artifact_repository import ArtifactRepository, ArtifactWriteScope
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.read_cursors import OpenReadCursor, ReadCursorStore
from mote.runtime.fileops.text_layout import text_page
from mote.runtime.fileops.text_sources import MaterializedText, TextSourceService

_DEFAULT_TEXT_LINES = 2_000
_MAX_TEXT_LINES = 10_000


@dataclass(frozen=True)
class _CursorSource:
    source: MaterializedText
    manifest: BlobRef
    offset: int


class TextViewService:
    """Pages one materialized version without reopening the source path."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        cursors: ReadCursorStore,
        sources: TextSourceService,
    ) -> None:
        self.artifacts = artifacts
        self.cursors = cursors
        self.sources = sources

    def read(
        self,
        path: str,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        continuation: Optional[OpenReadCursor] = None,
        scope: ArtifactWriteScope | None = None,
        expected_epoch: int,
    ) -> FileTextView:
        if offset is not None and offset < 1:
            raise FileReadRangeError(
                "text line offset must be at least 1",
                offset=offset,
            )
        if limit is not None and limit < 0:
            raise FileReadRangeError(
                "text line limit must be non-negative",
                limit=limit,
            )
        effective_limit = limit or _DEFAULT_TEXT_LINES
        if effective_limit > _MAX_TEXT_LINES:
            raise FileReadRangeError(
                f"text view accepts at most {_MAX_TEXT_LINES} lines per call",
                limit=effective_limit,
                maximum=_MAX_TEXT_LINES,
            )

        manifest = None
        if continuation is not None:
            if scope is not None:
                raise ReadCursorError("text continuation cannot create artifacts")
            if offset is not None:
                raise ReadCursorError("text cursor cannot be combined with an explicit offset")
            if encoding is not None or fallback_encoding is not None:
                raise ReadCursorError("text cursor already fixes the encoding decision")
            resumed = self._resume(path, continuation)
            source = resumed.source
            manifest = resumed.manifest
            effective_offset = resumed.offset
        else:
            if scope is None:
                raise ReadCursorError("initial text read requires an artifact scope")
            effective_offset = 1 if offset is None else offset
            source = self.sources.materialize(
                path,
                scope=scope,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
                maximum_bytes=MAX_MATERIALIZED_TEXT_BYTES,
            )
            if source.mode == TextViewMode.DOCUMENT and (encoding is not None or fallback_encoding is not None):
                raise EncodingRejectedError(
                    "encoding controls do not apply to extracted documents",
                    path=path,
                )

        lines, total_lines = text_page(
            source.text,
            offset=effective_offset,
            limit=effective_limit,
        )
        continuation_offset = effective_offset + len(lines)
        next_offset = continuation_offset if lines and continuation_offset <= total_lines else None
        next_cursor = None
        if next_offset is not None:
            if manifest is None:
                if scope is None:
                    raise ReadCursorError("text cursor manifest has no artifact scope")
                manifest = self._persist_manifest(source, scope)
            next_cursor = (
                self.cursors.advance(continuation, next_offset)
                if continuation is not None
                else self.cursors.issue(
                    manifest,
                    next_offset,
                    expected_epoch=expected_epoch,
                )
            )
        self.cursors.observe(source.snapshot, expected_epoch=expected_epoch)
        if scope is not None:
            scope.complete(durability_root=self.cursors.registry.path.parent)
        return FileTextView(
            snapshot=source.snapshot,
            mode=source.mode,
            status=(ReadViewStatus.PARTIAL if next_cursor is not None else ReadViewStatus.COMPLETE),
            offset=effective_offset,
            next_offset=next_offset,
            total_lines=total_lines,
            lines=lines,
            next_cursor=next_cursor,
        )

    def _persist_manifest(
        self,
        source: MaterializedText,
        scope: ArtifactWriteScope,
    ) -> BlobRef:
        text_bytes = source.text.encode("utf-8", errors="strict")
        if len(text_bytes) > MAX_MATERIALIZED_TEXT_BYTES:
            raise FileReadRangeError(
                f"materialized text exceeds {MAX_MATERIALIZED_TEXT_BYTES} bytes",
                size=len(text_bytes),
                maximum=MAX_MATERIALIZED_TEXT_BYTES,
            )
        text_artifact = scope.put_bytes(text_bytes)
        return self.cursors.persist(
            scope,
            ReadCursorKind.TEXT,
            {
                "snapshot": snapshot_to_dict(source.snapshot),
                "text_artifact": blob_to_dict(text_artifact),
                "mode": source.mode.value,
            },
        )

    def _resume(self, path: str, opened: OpenReadCursor) -> _CursorSource:
        try:
            if opened.kind != ReadCursorKind.TEXT:
                raise ValueError("cursor is not a text continuation")
            snapshot = snapshot_from_dict(opened.payload["snapshot"])
            text_artifact = blob_from_dict(opened.payload["text_artifact"])
            if text_artifact is None:
                raise ValueError("text artifact is missing")
            mode = TextViewMode(str(opened.payload["mode"]))
            if opened.position < 1:
                raise ValueError("text cursor offset is invalid")
            if path_token(path).native != snapshot.requested_path.native:
                raise ValueError("cursor belongs to a different file")
            self.artifacts.verify(snapshot.artifact)
            text = self.artifacts.read_bytes(text_artifact).decode(
                "utf-8",
                errors="strict",
            )
            if mode == TextViewMode.TEXT and snapshot.encoding is None:
                raise ValueError("text cursor has no encoding decision")
            if mode == TextViewMode.DOCUMENT and snapshot.encoding is not None:
                raise ValueError("document cursor contains a text encoding")
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            SnapshotDurabilityError,
        ) as exc:
            raise ReadCursorError("text cursor manifest is invalid", cause=exc) from exc
        return _CursorSource(
            source=MaterializedText(
                snapshot=snapshot,
                mode=mode,
                text=text,
            ),
            manifest=opened.manifest,
            offset=opened.position,
        )


__all__ = ["TextViewService"]
