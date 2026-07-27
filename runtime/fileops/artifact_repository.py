"""Reservation-only durable content-addressed artifact repository."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from mote.contracts.fileops.errors import SnapshotDurabilityError
from mote.contracts.fileops.models import BlobRef, LockMode, LockSpec
from mote.runtime.fileops.artifact_lifecycle import (
    ArtifactLifecycleCatalog,
    ArtifactLifecycleConflictError,
    ArtifactObject,
    ArtifactObjectState,
    ArtifactReservation,
    ArtifactStage,
)
from mote.runtime.fileops.locking import ARTIFACT_LOCK_LEVEL, HierarchicalLockManager

_CHUNK_SIZE = 1_024 * 1_024


class ArtifactReclaimStatus(StrEnum):
    RECLAIMED = "reclaimed"
    ALREADY_RECLAIMED = "already_reclaimed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ArtifactReclaimResult:
    candidate: ArtifactObject
    status: ArtifactReclaimStatus


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise SnapshotDurabilityError(
            "cannot open artifact directory for fsync",
            path=str(path),
            cause=exc,
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise SnapshotDurabilityError(
            "cannot fsync artifact directory",
            path=str(path),
            cause=exc,
        ) from exc
    finally:
        os.close(fd)


class ArtifactRepository:
    """Combines lifecycle admission with verified immutable payload storage."""

    def __init__(self, root: Path, *, hard_limit_bytes: int) -> None:
        self.root = Path(root)
        self.incoming_root = self.root / ".incoming"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.incoming_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
            os.chmod(self.incoming_root, 0o700)
        self.catalog = ArtifactLifecycleCatalog(
            self.root,
            hard_limit_bytes=hard_limit_bytes,
        )
        self.locks = HierarchicalLockManager(self.catalog.control_root / "locks")

    def reserve(
        self,
        max_bytes: int,
        owner: str,
        ttl_seconds: float,
    ) -> ArtifactReservation:
        return self.catalog.reserve(max_bytes, owner, ttl_seconds)

    def renew(
        self,
        reservation: ArtifactReservation | str,
        ttl_seconds: float,
    ) -> ArtifactReservation:
        return self.catalog.renew(reservation, ttl_seconds)

    def stage(
        self,
        reservation: ArtifactReservation | str,
        maximum_bytes: int,
    ) -> ArtifactStage:
        return self.catalog.stage(reservation, maximum_bytes)

    def release(
        self,
        reservation: ArtifactReservation | str,
    ) -> ArtifactReservation:
        return self.catalog.release(reservation)

    def write_scope(
        self,
        *,
        owner: str,
        maximum_bytes: int,
        ttl_seconds: float,
    ) -> "ArtifactWriteScope":
        return ArtifactWriteScope(
            self,
            owner=owner,
            maximum_bytes=maximum_bytes,
            ttl_seconds=ttl_seconds,
        )

    def capture(self, stage: ArtifactStage) -> "ArtifactCapture":
        if type(stage) is not ArtifactStage:
            raise TypeError("artifact capture requires an explicit stage")
        if stage.artifact is not None or stage.allocation_bytes < 0:
            raise ArtifactLifecycleConflictError(
                "artifact capture stage is not open",
                stage_id=stage.stage_id,
            )
        return ArtifactCapture(self, stage)

    def put(self, stage: ArtifactStage, chunks: Iterable[bytes]) -> BlobRef:
        """Streams one explicitly staged artifact through the bounded capture path."""
        with self.capture(stage) as capture:
            for chunk in chunks:
                capture.write(chunk)
            return capture.seal()

    def resolve_live(self, digest: str) -> BlobRef:
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("artifact digest is invalid")
        lifecycle = self.catalog.object(digest)
        if lifecycle is None or lifecycle.state != ArtifactObjectState.LIVE:
            raise SnapshotDurabilityError(
                "artifact digest does not resolve to a live object",
                digest=digest,
            )
        self.verify(lifecycle.artifact)
        return lifecycle.artifact

    def open_verified(self, artifact: BlobRef) -> "VerifiedArtifactStream":
        self._validate_ref(artifact)
        return VerifiedArtifactStream(self, artifact)

    def reclaim(self, candidate: ArtifactObject) -> ArtifactReclaimResult:
        """Durably remove one exact DELETING payload and catalog record."""
        if type(candidate) is not ArtifactObject:
            raise TypeError("artifact deletion requires an exact lifecycle object")
        if candidate.state != ArtifactObjectState.DELETING:
            raise ArtifactLifecycleConflictError(
                "artifact deletion candidate is not deleting",
                digest=candidate.artifact.digest,
                state=candidate.state.value,
            )
        self._validate_ref(candidate.artifact)
        payload = self._payload_path(candidate.artifact)
        with self.locks.acquire_many((self._artifact_lock(candidate.artifact, LockMode.EXCLUSIVE),)):
            current = self.catalog.object(candidate.artifact.digest)
            if current is None:
                try:
                    os.lstat(payload)
                except FileNotFoundError:
                    return ArtifactReclaimResult(
                        candidate,
                        ArtifactReclaimStatus.ALREADY_RECLAIMED,
                    )
                except OSError as exc:
                    raise SnapshotDurabilityError(
                        "cannot inspect completed artifact deletion",
                        digest=candidate.artifact.digest,
                        cause=exc,
                    ) from exc
                raise ArtifactLifecycleConflictError(
                    "deleted artifact still has a payload",
                    digest=candidate.artifact.digest,
                )
            if current != candidate:
                return ArtifactReclaimResult(
                    candidate,
                    ArtifactReclaimStatus.SUPERSEDED,
                )
            try:
                payload.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SnapshotDurabilityError(
                    "cannot delete artifact payload",
                    digest=candidate.artifact.digest,
                    cause=exc,
                ) from exc
            _fsync_directory(payload.parent)
            self.catalog.complete_deletion(candidate.artifact)
            return ArtifactReclaimResult(
                candidate,
                ArtifactReclaimStatus.RECLAIMED,
            )

    def read_bytes(self, artifact: BlobRef) -> bytes:
        data = bytearray()
        with self.open_verified(artifact) as stream:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                data.extend(chunk)
        return bytes(data)

    def read_bounded(self, artifact: BlobRef, *, maximum_bytes: int) -> bytes:
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("artifact read limit must be a non-negative integer")
        self._require_live(artifact)
        if artifact.size > maximum_bytes:
            raise SnapshotDurabilityError(
                "live artifact exceeds the bounded read policy",
                digest=artifact.digest,
                size=artifact.size,
                maximum=maximum_bytes,
            )
        return self.read_bytes(artifact)

    def read_range(
        self,
        artifact: BlobRef,
        *,
        offset: int,
        limit: int,
    ) -> bytes:
        if type(offset) is not int or offset < 0:
            raise ValueError("artifact range offset must be a non-negative integer")
        if type(limit) is not int or limit < 0:
            raise ValueError("artifact range limit must be a non-negative integer")
        observed = 0
        selected = bytearray()
        with self.open_verified(artifact) as stream:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                start = observed
                end = start + len(chunk)
                observed = end
                selected_start = max(offset, start)
                selected_end = min(offset + limit, end)
                if selected_start < selected_end:
                    selected.extend(chunk[selected_start - start : selected_end - start])
        return bytes(selected)

    def iter_lines(self, artifact: BlobRef) -> Iterator[bytes]:
        with tempfile.TemporaryFile() as spool:
            with self.open_verified(artifact) as stream:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    spool.write(chunk)
            spool.seek(0)
            yield from spool

    def verify(self, artifact: BlobRef) -> None:
        with self.open_verified(artifact) as stream:
            while stream.read(_CHUNK_SIZE):
                pass

    def _publish_capture(
        self,
        stage: ArtifactStage,
        temp_path: Path,
        artifact: BlobRef,
    ) -> BlobRef:
        try:
            with self.locks.acquire_many((self._artifact_lock(artifact, LockMode.EXCLUSIVE),)):
                destination = self._payload_path(artifact)
                parent_existed = destination.parent.exists()
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name == "posix":
                    os.chmod(destination.parent, 0o700)
                if destination.exists():
                    self._verify_path(destination, artifact)
                    temp_path.unlink()
                else:
                    try:
                        os.link(temp_path, destination)
                        temp_path.unlink()
                    except FileExistsError:
                        self._verify_path(destination, artifact)
                        temp_path.unlink()
                    except OSError:
                        if destination.exists():
                            self._verify_path(destination, artifact)
                            temp_path.unlink()
                        else:
                            os.replace(temp_path, destination)
                self._verify_path(destination, artifact)
                _fsync_directory(destination.parent)
                if not parent_existed:
                    _fsync_directory(self.root)
                self._record_after_seal(stage, artifact)
                live = self.catalog.mark_live(stage, artifact)
        except Exception as exc:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            if isinstance(exc, SnapshotDurabilityError):
                raise
            raise SnapshotDurabilityError(
                "cannot durably publish staged artifact",
                digest=artifact.digest,
                stage_id=stage.stage_id,
                cause=exc,
            ) from exc
        if live.state != ArtifactObjectState.LIVE or live.artifact != artifact:
            raise SnapshotDurabilityError(
                "artifact lifecycle did not publish the sealed payload",
                digest=artifact.digest,
                stage_id=stage.stage_id,
            )
        return artifact

    def _record_after_seal(self, stage: ArtifactStage, artifact: BlobRef) -> None:
        try:
            self.catalog.record_staged(stage, artifact)
        except ArtifactLifecycleConflictError:
            existing = self.catalog.object(artifact.digest)
            if existing is None or existing.state != ArtifactObjectState.STAGING:
                raise
            self.catalog.recover()
            self.catalog.record_staged(stage, artifact)

    def _require_live(self, artifact: BlobRef) -> None:
        self._validate_ref(artifact)
        lifecycle = self.catalog.object(artifact.digest)
        if lifecycle is None:
            raise SnapshotDurabilityError(
                "artifact is not registered",
                digest=artifact.digest,
            )
        if lifecycle.artifact != artifact:
            raise SnapshotDurabilityError(
                "artifact reference conflicts with the lifecycle catalog",
                digest=artifact.digest,
                expected_size=lifecycle.artifact.size,
                actual_size=artifact.size,
            )
        if lifecycle.state != ArtifactObjectState.LIVE:
            raise SnapshotDurabilityError(
                "artifact is not live",
                digest=artifact.digest,
                state=lifecycle.state.value,
            )

    def _payload_path(self, artifact: BlobRef) -> Path:
        return self.root / artifact.digest[:2] / artifact.digest

    @staticmethod
    def _artifact_lock(artifact: BlobRef, mode: LockMode) -> LockSpec:
        return LockSpec(
            ARTIFACT_LOCK_LEVEL,
            artifact.digest,
            mode,
            f"artifact {artifact.digest}",
        )

    @classmethod
    def _verify_path(cls, path: Path, artifact: BlobRef) -> None:
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise SnapshotDurabilityError(
                "cannot verify sealed artifact payload",
                digest=artifact.digest,
                cause=exc,
            ) from exc
        cls._verify_observed(artifact, size, digest.hexdigest())

    @staticmethod
    def _verify_observed(artifact: BlobRef, size: int, digest: str) -> None:
        if size != artifact.size or digest != artifact.digest:
            raise SnapshotDurabilityError(
                "artifact payload failed integrity verification",
                digest=artifact.digest,
                expected_size=artifact.size,
                actual_size=size,
                actual_digest=digest,
            )

    @staticmethod
    def _validate_ref(artifact: BlobRef) -> None:
        if type(artifact) is not BlobRef:
            raise TypeError("artifact reference is invalid")
        if (
            type(artifact.digest) is not str
            or len(artifact.digest) != 64
            or any(character not in "0123456789abcdef" for character in artifact.digest)
        ):
            raise ValueError("artifact digest is invalid")
        if type(artifact.size) is not int or artifact.size < 0:
            raise ValueError("artifact size is invalid")


class ArtifactCapture(AbstractContextManager["ArtifactCapture"]):
    """One bounded writer owned by an explicit durable lifecycle stage."""

    def __init__(self, repository: ArtifactRepository, stage: ArtifactStage) -> None:
        self.repository = repository
        self.stage = stage
        self._stream: BinaryIO | None = None
        self._temp_path: Path | None = None
        self._digest = hashlib.sha256()
        self._size = 0
        self._sealed = False

    @property
    def size(self) -> int:
        return self._size

    def __enter__(self) -> "ArtifactCapture":
        if self._stream is not None or self._sealed:
            raise RuntimeError("artifact capture cannot be entered twice")
        fd, raw_path = tempfile.mkstemp(
            prefix=f"stage-{self.stage.stage_id}-",
            suffix=".tmp",
            dir=self.repository.incoming_root,
        )
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        self._temp_path = Path(raw_path)
        self._stream = os.fdopen(fd, "wb", closefd=True)
        return self

    def write(self, chunk: bytes) -> None:
        if self._stream is None or self._sealed:
            raise RuntimeError("artifact capture is not writable")
        if type(chunk) is not bytes:
            raise TypeError("artifact chunks must be bytes")
        next_size = self._size + len(chunk)
        if next_size > self.stage.allocation_bytes:
            raise SnapshotDurabilityError(
                "artifact stream exceeds its stage allocation",
                stage_id=self.stage.stage_id,
                allocation=self.stage.allocation_bytes,
                attempted_size=next_size,
            )
        self._stream.write(chunk)
        self._digest.update(chunk)
        self._size = next_size

    def seal(self) -> BlobRef:
        if self._stream is None or self._temp_path is None or self._sealed:
            raise RuntimeError("artifact capture is not sealable")
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
            artifact = BlobRef(digest=self._digest.hexdigest(), size=self._size)
            result = self.repository._publish_capture(
                self.stage,
                self._temp_path,
                artifact,
            )
            self._temp_path = None
            self._sealed = True
            return result
        except Exception:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._temp_path is not None:
            try:
                self._temp_path.unlink()
            except FileNotFoundError:
                pass
            self._temp_path = None
        if not self._sealed:
            try:
                self.repository.catalog.abort_stage(self.stage)
            except ArtifactLifecycleConflictError:
                if exc_type is None:
                    raise
        return None


class VerifiedArtifactStream(AbstractContextManager["VerifiedArtifactStream"]):
    """A single stable payload handle that verifies all bytes consumed through it."""

    def __init__(self, repository: ArtifactRepository, artifact: BlobRef) -> None:
        self.repository = repository
        self.artifact = artifact
        self._stream: BinaryIO | None = None
        self._lock_lease: AbstractContextManager[None] | None = None
        self._digest = hashlib.sha256()
        self._size = 0
        self._verified = False

    def __enter__(self) -> "VerifiedArtifactStream":
        if self._stream is not None or self._verified:
            raise RuntimeError("verified artifact stream cannot be entered twice")
        self._lock_lease = self.repository.locks.acquire_many(
            (self.repository._artifact_lock(self.artifact, LockMode.SHARED),)
        )
        self._lock_lease.__enter__()
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        path = self.repository._payload_path(self.artifact)
        try:
            self.repository._require_live(self.artifact)
            fd = os.open(path, flags)
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size != self.artifact.size:
                os.close(fd)
                raise SnapshotDurabilityError(
                    "live artifact payload is not the expected regular file",
                    digest=self.artifact.digest,
                    expected_size=self.artifact.size,
                    actual_size=stat_result.st_size,
                )
            self._stream = os.fdopen(fd, "rb", closefd=True)
        except SnapshotDurabilityError:
            self._release_lock()
            raise
        except OSError as exc:
            self._release_lock()
            raise SnapshotDurabilityError(
                "cannot open live artifact payload",
                digest=self.artifact.digest,
                cause=exc,
            ) from exc
        return self

    def read(self, size: int = -1) -> bytes:
        if self._stream is None or self._verified:
            raise RuntimeError("verified artifact stream is not readable")
        if type(size) is not int or size < -1:
            raise ValueError("artifact stream read size is invalid")
        try:
            chunk = self._stream.read(size)
        except OSError as exc:
            raise SnapshotDurabilityError(
                "cannot read live artifact payload",
                digest=self.artifact.digest,
                cause=exc,
            ) from exc
        if chunk:
            self._digest.update(chunk)
            self._size += len(chunk)
            if self._size > self.artifact.size:
                raise SnapshotDurabilityError(
                    "live artifact payload exceeds its catalog size",
                    digest=self.artifact.digest,
                    expected_size=self.artifact.size,
                    actual_size=self._size,
                )
            return chunk
        self.repository._verify_observed(
            self.artifact,
            self._size,
            self._digest.hexdigest(),
        )
        self._verified = True
        return b""

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            if exc_type is None and not self._verified:
                raise SnapshotDurabilityError(
                    "verified artifact stream exited before reaching EOF",
                    digest=self.artifact.digest,
                    consumed=self._size,
                    expected_size=self.artifact.size,
                )
        finally:
            self._release_lock()
        return None

    def _release_lock(self) -> None:
        if self._lock_lease is not None:
            self._lock_lease.__exit__(None, None, None)
            self._lock_lease = None


class ArtifactWriteScopeState(StrEnum):
    RESERVED = "reserved"
    ACTIVE = "active"
    COMPLETED = "completed"
    RELEASED = "released"
    DISCARDED = "discarded"
    ABORTED = "aborted"
    CLEANUP_FAILED = "cleanup_failed"


class ArtifactWriteScope(AbstractContextManager["ArtifactWriteScope"]):
    """One explicitly budgeted unit of durable artifact publication."""

    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        owner: str,
        maximum_bytes: int,
        ttl_seconds: float,
    ) -> None:
        if type(repository) is not ArtifactRepository:
            raise TypeError("artifact write scope requires an artifact repository")
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("artifact write scope maximum must be non-negative")
        self.repository = repository
        self.maximum_bytes = maximum_bytes
        self.reservation = repository.reserve(
            maximum_bytes,
            owner,
            ttl_seconds,
        )
        self.state = ArtifactWriteScopeState.RESERVED
        self._written_bytes = 0
        self._stages: list[ArtifactStage] = []
        self._artifacts: list[BlobRef] = []

    @property
    def written_bytes(self) -> int:
        return self._written_bytes

    @property
    def remaining_bytes(self) -> int:
        return self.maximum_bytes - self._written_bytes

    @property
    def artifacts(self) -> tuple[BlobRef, ...]:
        return tuple(self._artifacts)

    def __enter__(self) -> "ArtifactWriteScope":
        if self.state != ArtifactWriteScopeState.RESERVED:
            raise ArtifactLifecycleConflictError(
                "artifact write scope cannot be entered",
                state=self.state.value,
            )
        self.state = ArtifactWriteScopeState.ACTIVE
        return self

    def put_bytes(self, data: bytes) -> BlobRef:
        if type(data) is not bytes:
            raise TypeError("artifact scope data must be bytes")
        return self.put_chunks((data,), maximum_bytes=len(data))

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        maximum_bytes: int,
    ) -> BlobRef:
        self._require_active()
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("artifact scope stream maximum must be non-negative")
        if maximum_bytes > self.remaining_bytes:
            raise SnapshotDurabilityError(
                "artifact scope stream exceeds the remaining total budget",
                maximum=maximum_bytes,
                remaining=self.remaining_bytes,
                scope_maximum=self.maximum_bytes,
            )
        stage = self.repository.stage(self.reservation, maximum_bytes)
        self._stages.append(stage)
        artifact = self.repository.put(stage, chunks)
        self._written_bytes += artifact.size
        self._artifacts.append(artifact)
        return artifact

    def complete(self, *, durability_root: Path) -> None:
        self._require_active()
        root = Path(durability_root)
        _fsync_directory(root)
        self.state = ArtifactWriteScopeState.COMPLETED

    def discard(self) -> None:
        self._require_active()
        self.state = ArtifactWriteScopeState.DISCARDED

    def __exit__(self, exc_type, exc, traceback) -> None:
        completed = self.state == ArtifactWriteScopeState.COMPLETED
        discarded = self.state == ArtifactWriteScopeState.DISCARDED
        try:
            if not completed and not discarded:
                self._abort_open_stages()
            self.repository.release(self.reservation)
        except Exception as cleanup_error:
            self.state = ArtifactWriteScopeState.CLEANUP_FAILED
            if exc is not None:
                raise cleanup_error from exc
            raise
        if completed:
            self.state = ArtifactWriteScopeState.RELEASED
            return None
        if discarded:
            return None
        self.state = ArtifactWriteScopeState.ABORTED
        if exc_type is None:
            raise ArtifactLifecycleConflictError(
                "artifact write scope exited without complete",
                reservation_id=self.reservation.reservation_id,
            )
        return None

    def _abort_open_stages(self) -> None:
        self.repository.catalog.recover()
        for stage in reversed(self._stages):
            try:
                self.repository.catalog.abort_stage(stage)
            except ArtifactLifecycleConflictError:
                pass

    def _require_active(self) -> None:
        if self.state != ArtifactWriteScopeState.ACTIVE:
            raise ArtifactLifecycleConflictError(
                "artifact write scope is not active",
                state=self.state.value,
            )


__all__ = [
    "ArtifactCapture",
    "ArtifactRepository",
    "ArtifactWriteScope",
    "ArtifactWriteScopeState",
    "VerifiedArtifactStream",
]
