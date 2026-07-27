"""Exact v1 file-history decoding and artifact import.

This module is deliberately isolated from the current session event model.  A
v1 ``file_snapshot`` record proves only a before-image; it does not contain the
after-image, filesystem identity, metadata, or committed version required by a
managed file transaction.  Migration therefore produces an
the current typed history event and never fabricates a transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Protocol

from mote.contracts.fileops.events import FileHistoryImportedEvent
from mote.contracts.fileops.models import BlobRef
from mote.runtime.fileops.artifact_lifecycle import ArtifactReservation, ArtifactReservationState
from mote.runtime.fileops.artifact_repository import ArtifactCapture, ArtifactRepository

_CHUNK_SIZE = 1024 * 1024
_FILE_SNAPSHOT_FIELDS = {
    "path",
    "operation",
    "pre_hash",
    "pre_size",
    "display_path",
    "tool",
    "backend",
}
_HEX = frozenset("0123456789abcdef")


class V1MigrationError(ValueError):
    """A v1 record or referenced artifact cannot be migrated exactly."""


class V1SnapshotOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class V1SnapshotBackend(StrEnum):
    BLOB = "blob"
    GIT = "git"


class _Digest(Protocol):
    def update(self, data: bytes) -> None:
        ...

    def hexdigest(self) -> str:
        ...


@dataclass(frozen=True)
class V1FileSnapshot:
    """The exact payload of the historic rollout ``file_snapshot`` event."""

    path: str
    operation: V1SnapshotOperation
    pre_hash: Optional[str]
    pre_size: int
    display_path: str
    tool: str
    backend: V1SnapshotBackend

    def __post_init__(self) -> None:
        for name, value in (
            ("path", self.path),
            ("display_path", self.display_path),
        ):
            if type(value) is not str or not value:
                raise V1MigrationError(f"v1 file_snapshot {name} is invalid")
        if type(self.tool) is not str:
            raise V1MigrationError("v1 file_snapshot tool is invalid")
        if type(self.pre_size) is not int or self.pre_size < 0:
            raise V1MigrationError("v1 file_snapshot pre_size is invalid")
        if not isinstance(self.operation, V1SnapshotOperation):
            raise V1MigrationError("v1 file_snapshot operation is invalid")
        if not isinstance(self.backend, V1SnapshotBackend):
            raise V1MigrationError("v1 file_snapshot backend is invalid")
        if self.operation == V1SnapshotOperation.CREATE:
            if self.pre_hash is not None or self.pre_size != 0:
                raise V1MigrationError("v1 create snapshot must describe an absent before-image")
            return
        digest_length = 64 if self.backend == V1SnapshotBackend.BLOB else 40
        if type(self.pre_hash) is not str or not _is_hex_digest(self.pre_hash, digest_length):
            raise V1MigrationError("v1 update snapshot digest is invalid")


def parse_v1_file_snapshot(payload: object) -> V1FileSnapshot:
    """Decode one v1 payload, rejecting missing, extra, or coerced fields."""

    if type(payload) is not dict or set(payload) != _FILE_SNAPSHOT_FIELDS:
        raise V1MigrationError("v1 file_snapshot fields are not canonical")

    path = _required_text(payload, "path")
    display_path = _required_text(payload, "display_path")
    tool = _text(payload, "tool")
    pre_size = payload["pre_size"]
    if type(pre_size) is not int or pre_size < 0:
        raise V1MigrationError("v1 file_snapshot pre_size is invalid")

    try:
        operation = V1SnapshotOperation(_required_text(payload, "operation"))
    except ValueError as exc:
        raise V1MigrationError("v1 file_snapshot operation is invalid") from exc
    try:
        backend = V1SnapshotBackend(_required_text(payload, "backend"))
    except ValueError as exc:
        raise V1MigrationError("v1 file_snapshot backend is invalid") from exc

    pre_hash = payload["pre_hash"]
    if operation == V1SnapshotOperation.CREATE:
        if pre_hash is not None or pre_size != 0:
            raise V1MigrationError("v1 create snapshot must describe an absent before-image")
    else:
        digest_length = 64 if backend == V1SnapshotBackend.BLOB else 40
        if type(pre_hash) is not str or not _is_hex_digest(pre_hash, digest_length):
            raise V1MigrationError("v1 update snapshot digest is invalid")

    return V1FileSnapshot(
        path=path,
        operation=operation,
        pre_hash=pre_hash,
        pre_size=pre_size,
        display_path=display_path,
        tool=tool,
        backend=backend,
    )


def import_v1_file_snapshot(
    snapshot: V1FileSnapshot,
    *,
    session_id: str,
    source_ordinal: int,
    recorded_at: str,
    session_dir: Path,
    repository: ArtifactRepository,
    reservation: ArtifactReservation,
) -> FileHistoryImportedEvent:
    """Verify a v1 before-image and import it into the canonical artifact store."""

    if not isinstance(snapshot, V1FileSnapshot):
        raise TypeError("v1 snapshot import requires a V1FileSnapshot")
    if type(session_id) is not str or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if type(source_ordinal) is not int or source_ordinal < 1:
        raise ValueError("source_ordinal must be a positive integer")
    if type(recorded_at) is not str or not recorded_at:
        raise ValueError("recorded_at must be a non-empty string")
    if not isinstance(session_dir, Path):
        raise TypeError("session_dir must be a Path")
    if not isinstance(repository, ArtifactRepository):
        raise TypeError("repository must be an ArtifactRepository")
    _validate_reservation(snapshot, reservation)

    try:
        before = None
        if snapshot.operation == V1SnapshotOperation.UPDATE:
            before = _import_before_image(
                snapshot,
                session_dir,
                repository,
                reservation,
            )

        identity = json.dumps(
            {
                "backend": snapshot.backend.value,
                "display_path": snapshot.display_path,
                "operation": snapshot.operation.value,
                "path": snapshot.path,
                "pre_hash": snapshot.pre_hash,
                "pre_size": snapshot.pre_size,
                "recorded_at": recorded_at,
                "session_id": session_id,
                "source_ordinal": source_ordinal,
                "tool": snapshot.tool,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        import_id = hashlib.sha256(b"mote-session-v1-file-history\0" + identity).hexdigest()
        fact = FileHistoryImportedEvent(
            import_id=import_id,
            source_ordinal=source_ordinal,
            recorded_at=recorded_at,
            path=snapshot.path,
            display_path=snapshot.display_path,
            operation=snapshot.operation.value,
            before=before,
            source=snapshot.tool,
            source_schema_version=1,
        )
    except Exception:
        repository.release(reservation)
        raise
    repository.release(reservation)
    return fact


def _import_before_image(
    snapshot: V1FileSnapshot,
    session_dir: Path,
    repository: ArtifactRepository,
    reservation: ArtifactReservation,
) -> BlobRef:
    assert snapshot.pre_hash is not None
    if snapshot.backend == V1SnapshotBackend.BLOB:
        source = session_dir / "blobs" / snapshot.pre_hash[:2] / snapshot.pre_hash
        return _import_blob_file(
            source,
            expected_digest=snapshot.pre_hash,
            expected_size=snapshot.pre_size,
            repository=repository,
            reservation=reservation,
        )
    return _import_git_blob(
        session_dir / "git",
        expected_digest=snapshot.pre_hash,
        expected_size=snapshot.pre_size,
        repository=repository,
        reservation=reservation,
    )


def _import_blob_file(
    source: Path,
    *,
    expected_digest: str,
    expected_size: int,
    repository: ArtifactRepository,
    reservation: ArtifactReservation,
) -> BlobRef:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise V1MigrationError(f"cannot open v1 blob before-image {expected_digest}") from exc
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise V1MigrationError("v1 blob before-image is not a regular file")
        if source_stat.st_size != expected_size:
            raise V1MigrationError("v1 blob before-image size does not match its event")
        stage = repository.stage(reservation, expected_size)
        with os.fdopen(source_fd, "rb", closefd=False) as stream:
            with repository.capture(stage) as capture:
                _write_verified(
                    stream,
                    capture,
                    expected_digest=expected_digest,
                    expected_size=expected_size,
                    legacy_hash=hashlib.sha256(),
                )
                artifact = capture.seal()
    finally:
        os.close(source_fd)
    return artifact


def _import_git_blob(
    git_dir: Path,
    *,
    expected_digest: str,
    expected_size: int,
    repository: ArtifactRepository,
    reservation: ArtifactReservation,
) -> BlobRef:
    try:
        process = subprocess.Popen(
            ["git", "--git-dir", str(git_dir), "cat-file", "blob", expected_digest],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise V1MigrationError(f"cannot start git to read v1 before-image {expected_digest}") from exc
    try:
        if process.stdout is None or process.stderr is None:
            raise V1MigrationError("git v1 before-image process has no output pipes")
        stage = repository.stage(reservation, expected_size)
        legacy_hash = hashlib.sha1(usedforsecurity=False)
        legacy_hash.update(f"blob {expected_size}\0".encode("ascii"))
        with repository.capture(stage) as capture:
            _write_verified(
                process.stdout,
                capture,
                expected_digest=expected_digest,
                expected_size=expected_size,
                legacy_hash=legacy_hash,
            )
            stderr = process.stderr.read()
            return_code = process.wait()
            if return_code != 0:
                raise V1MigrationError(
                    "cannot read v1 git before-image: " + stderr.decode("utf-8", errors="surrogateescape").strip()
                )
            return capture.seal()
    except Exception:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _write_verified(
    stream: BinaryIO,
    capture: ArtifactCapture,
    *,
    expected_digest: str,
    expected_size: int,
    legacy_hash: _Digest,
) -> None:
    size = 0
    for chunk in _chunks(stream):
        size += len(chunk)
        if size > expected_size:
            raise V1MigrationError("v1 before-image exceeds the size declared by its event")
        capture.write(chunk)
        legacy_hash.update(chunk)
    if size != expected_size:
        raise V1MigrationError("v1 before-image size does not match its event")
    if legacy_hash.hexdigest() != expected_digest:
        raise V1MigrationError("v1 before-image digest does not match its event")


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _required_text(payload: dict, key: str) -> str:
    value = payload[key]
    if type(value) is not str or not value:
        raise V1MigrationError(f"v1 file_snapshot {key} is invalid")
    return value


def _text(payload: dict, key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise V1MigrationError(f"v1 file_snapshot {key} is invalid")
    return value


def _is_hex_digest(value: str, length: int) -> bool:
    return len(value) == length and all(character in _HEX for character in value)


def _validate_reservation(
    snapshot: V1FileSnapshot,
    reservation: ArtifactReservation,
) -> None:
    if type(reservation) is not ArtifactReservation:
        raise TypeError("reservation must be an explicit ArtifactReservation")
    if reservation.state != ArtifactReservationState.ACTIVE:
        raise ValueError("artifact reservation is not active")
    if reservation.capacity_bytes != snapshot.pre_size or reservation.remaining_bytes != snapshot.pre_size:
        raise ValueError("artifact reservation must carry the exact v1 before-image budget")


__all__ = [
    "V1FileSnapshot",
    "V1MigrationError",
    "V1SnapshotBackend",
    "V1SnapshotOperation",
    "import_v1_file_snapshot",
    "parse_v1_file_snapshot",
]
