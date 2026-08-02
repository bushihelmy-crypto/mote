"""Single durable cursor protocol for all paged Read views."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.codec import blob_from_dict, snapshot_from_dict
from mote.contracts.file.errors import ReadCursorError, SnapshotDurabilityError
from mote.contracts.file.identity import FileSnapshot
from mote.contracts.file.views import ReadCursorKind
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, FileMutationArtifactRepository
from mote.runtime.fileops.resource_limits import MAX_READ_MANIFEST_BYTES

_MANIFEST_FORMAT = 1
_MANIFEST_KEYS = frozenset({"format_version", "kind", "payload"})
_PAYLOAD_KEYS = {
    ReadCursorKind.TEXT: frozenset({"snapshot", "text_artifact", "mode"}),
    ReadCursorKind.RAW: frozenset({"snapshot"}),
    ReadCursorKind.HEX: frozenset({"snapshot"}),
    ReadCursorKind.PDF_TEXT: frozenset({"snapshot", "dpi", "adapter"}),
    ReadCursorKind.PDF_RENDER: frozenset({"snapshot", "dpi", "adapter"}),
}


@dataclass(frozen=True)
class OpenReadCursor:
    kind: ReadCursorKind
    payload: dict[str, Any]
    manifest: ContentIdentity
    position: int
    token: str


class ReadCursorStore:
    """Persists typed manifests and validates opaque continuation tokens."""

    def __init__(
        self,
        artifacts: FileMutationArtifactRepository,
        registry: DurableCursorRegistry,
    ) -> None:
        self.artifacts = artifacts
        self.registry = registry

    def persist(
        self,
        scope: ArtifactWriteScope,
        kind: ReadCursorKind,
        payload: dict[str, Any],
    ) -> ContentIdentity:
        self._validate_payload(kind, payload)
        raw = json.dumps(
            {
                "format_version": _MANIFEST_FORMAT,
                "kind": kind.value,
                "payload": payload,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > MAX_READ_MANIFEST_BYTES:
            raise ReadCursorError(
                "read cursor manifest exceeds the size limit",
                size=len(raw),
                maximum=MAX_READ_MANIFEST_BYTES,
            )
        return scope.put_bytes(raw)

    def observe(self, snapshot: FileSnapshot, *, expected_epoch: int) -> None:
        self.artifacts.verify(snapshot.artifact)
        self.artifacts.verify(snapshot.metadata)
        self.registry.observe(snapshot, expected_epoch=expected_epoch)

    def issue(
        self,
        manifest: ContentIdentity,
        position: int,
        *,
        expected_epoch: int,
    ) -> str:
        _, payload = self._load_manifest(manifest)
        return self.registry.issue(
            namespace="read",
            root_manifest=manifest,
            pinned_artifacts=self._pinned_artifacts(payload),
            position=position,
            expected_epoch=expected_epoch,
        )

    def advance(self, opened: OpenReadCursor, position: int) -> str:
        return self.registry.advance(
            opened.token,
            expected_namespace="read",
            position=position,
        )

    def open(self, cursor: str) -> OpenReadCursor:
        try:
            access = self.registry.open(cursor, expected_namespace="read")
            manifest = access.lease.root_manifest
            position = access.position
            kind, payload = self._load_manifest(manifest)
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
            SnapshotDurabilityError,
        ) as exc:
            raise ReadCursorError("read cursor manifest is invalid", cause=exc) from exc
        return OpenReadCursor(
            kind=kind,
            payload=payload,
            manifest=manifest,
            position=position,
            token=cursor,
        )

    def _load_manifest(
        self,
        manifest: ContentIdentity,
    ) -> tuple[ReadCursorKind, dict[str, Any]]:
        data = json.loads(
            self.artifacts.read_bounded(
                manifest,
                maximum_bytes=MAX_READ_MANIFEST_BYTES,
            ).decode("utf-8")
        )
        if type(data) is not dict or set(data) != _MANIFEST_KEYS:
            raise ValueError("read cursor manifest fields are not canonical")
        if type(data["format_version"]) is not int:
            raise TypeError("read cursor manifest format must be an integer")
        if data["format_version"] != _MANIFEST_FORMAT:
            raise ValueError("unsupported read cursor manifest format")
        if type(data["kind"]) is not str:
            raise TypeError("read cursor kind must be a string")
        kind = ReadCursorKind(data["kind"])
        payload = data["payload"]
        if type(payload) is not dict:
            raise TypeError("read cursor payload is not an object")
        self._validate_payload(kind, payload)
        return kind, payload

    @staticmethod
    def _pinned_artifacts(payload: dict[str, Any]) -> tuple[ContentIdentity, ...]:
        snapshot = snapshot_from_dict(payload["snapshot"])
        pins = [snapshot.artifact, snapshot.metadata]
        if "text_artifact" in payload:
            text_artifact = blob_from_dict(payload["text_artifact"])
            if text_artifact is None:
                raise ReadCursorError("read cursor text artifact is missing")
            pins.append(text_artifact)
        return tuple(pins)

    @staticmethod
    def _validate_payload(
        kind: ReadCursorKind,
        payload: dict[str, Any],
    ) -> None:
        if type(kind) is not ReadCursorKind:
            raise ReadCursorError("read cursor kind is invalid")
        if type(payload) is not dict or set(payload) != _PAYLOAD_KEYS[kind]:
            raise ReadCursorError("read cursor payload fields are not canonical")


__all__ = ["OpenReadCursor", "ReadCursorStore"]
