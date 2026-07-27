"""Lossless bounded byte views over sealed file snapshots."""

from __future__ import annotations

from typing import Optional

from mote.contracts.fileops.errors import FileReadRangeError, ReadCursorError
from mote.contracts.fileops.models import (
    BlobRef,
    ByteViewMode,
    FileByteView,
    FileSnapshot,
    ReadCursorKind,
    ReadViewStatus,
)
from mote.contracts.fileops.serialization import snapshot_from_dict, snapshot_to_dict
from mote.runtime.fileops.artifact_repository import ArtifactRepository, ArtifactWriteScope
from mote.runtime.fileops.capture import ManagedSnapshotCapture
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.read_cursors import OpenReadCursor, ReadCursorStore

_DEFAULT_RAW_BYTES = 4 * 1_024
_DEFAULT_HEX_BYTES = 256
_MAX_VIEW_BYTES = 1_024 * 1_024
_HEX_WIDTH = 16


class ByteViewService:
    """Pages one immutable byte artifact without reopening on continuation."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        capture: ManagedSnapshotCapture,
        cursors: ReadCursorStore,
    ) -> None:
        self.artifacts = artifacts
        self.capture = capture
        self.cursors = cursors

    def read(
        self,
        path: str,
        *,
        mode: ByteViewMode,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        continuation: Optional[OpenReadCursor] = None,
        scope: ArtifactWriteScope | None = None,
        expected_epoch: int,
    ) -> FileByteView:
        if offset is not None and offset < 0:
            raise FileReadRangeError("byte offset must be non-negative", offset=offset)
        if limit is not None and limit < 0:
            raise FileReadRangeError("byte limit must be non-negative", limit=limit)
        effective_limit = limit or (_DEFAULT_RAW_BYTES if mode == ByteViewMode.RAW else _DEFAULT_HEX_BYTES)
        if effective_limit > _MAX_VIEW_BYTES:
            raise FileReadRangeError(
                f"byte view limit exceeds {_MAX_VIEW_BYTES} bytes",
                limit=effective_limit,
                maximum=_MAX_VIEW_BYTES,
            )

        manifest = None
        if continuation is None:
            if scope is None:
                raise ReadCursorError("initial byte read requires an artifact scope")
            effective_offset = 0 if offset is None else offset
            snapshot = self.capture.capture(path, scope=scope)
        else:
            if scope is not None:
                raise ReadCursorError("byte continuation cannot create artifacts")
            if offset is not None:
                raise ReadCursorError("byte cursor cannot be combined with an explicit offset")
            snapshot, manifest, effective_offset = self._resume(
                path,
                mode,
                continuation,
            )
        data = self.artifacts.read_range(
            snapshot.artifact,
            offset=effective_offset,
            limit=effective_limit,
        )
        end = min(snapshot.version.size, effective_offset + len(data))
        next_offset = end if end < snapshot.version.size else None
        next_cursor = None
        if next_offset is not None:
            if manifest is None:
                if scope is None:
                    raise ReadCursorError("byte cursor manifest has no artifact scope")
                manifest = self.cursors.persist(
                    scope,
                    self._kind(mode),
                    {"snapshot": snapshot_to_dict(snapshot)},
                )
            next_cursor = (
                self.cursors.advance(continuation, next_offset)
                if continuation is not None
                else self.cursors.issue(
                    manifest,
                    next_offset,
                    expected_epoch=expected_epoch,
                )
            )
        self.cursors.observe(snapshot, expected_epoch=expected_epoch)
        if scope is not None:
            scope.complete(durability_root=self.cursors.registry.path.parent)
        return FileByteView(
            snapshot=snapshot,
            mode=mode,
            status=(ReadViewStatus.PARTIAL if next_cursor is not None else ReadViewStatus.COMPLETE),
            offset=effective_offset,
            next_offset=next_offset,
            total_bytes=snapshot.version.size,
            data=data,
            text=self._hex(data, effective_offset) if mode == ByteViewMode.HEX else "",
            next_cursor=next_cursor,
        )

    def _resume(
        self,
        path: str,
        mode: ByteViewMode,
        opened: OpenReadCursor,
    ) -> tuple[FileSnapshot, BlobRef, int]:
        try:
            if opened.kind != self._kind(mode):
                raise ValueError("cursor byte mode does not match the request")
            snapshot = snapshot_from_dict(opened.payload["snapshot"])
            if path_token(path).native != snapshot.requested_path.native:
                raise ValueError("cursor belongs to a different file")
            if opened.position > snapshot.version.size:
                raise ValueError("cursor byte offset exceeds the snapshot")
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadCursorError("byte cursor manifest is invalid", cause=exc) from exc
        return snapshot, opened.manifest, opened.position

    @staticmethod
    def _kind(mode: ByteViewMode) -> ReadCursorKind:
        return ReadCursorKind.RAW if mode == ByteViewMode.RAW else ReadCursorKind.HEX

    @staticmethod
    def _hex(data: bytes, offset: int) -> str:
        rows: list[str] = []
        for relative in range(0, len(data), _HEX_WIDTH):
            chunk = data[relative : relative + _HEX_WIDTH]
            hex_bytes = " ".join(f"{byte:02x}" for byte in chunk)
            gutter = "".join(chr(byte) if 0x20 <= byte <= 0x7E else "." for byte in chunk)
            rows.append(f"{offset + relative:016x}  {hex_bytes:<47}  |{gutter}|")
        return "\n".join(rows)


__all__ = ["ByteViewService"]
