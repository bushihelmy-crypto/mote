"""Metadata-preserving atomic publication of sealed artifacts."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from typing import Optional

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.errors import (
    ContentChangedError,
    FileOperationError,
    FilePublishError,
    IdentityChangedError,
    StaleSnapshotError,
    UnsupportedFilesystemSemanticsError,
)
from mote.contracts.file.identity import AbsentVersion, FileVersion, PresentVersion
from mote.runtime.fileops.identity import (
    PathLike,
    name_identity,
    native_path,
    same_open_file,
    target_identity_from_stat,
)
from mote.runtime.fileops.metadata import apply_metadata, capture_metadata
from mote.runtime.fileops.metadata_manifest import MAX_METADATA_MANIFEST_BYTES, decode_metadata_manifest
from mote.runtime.fileops.mutation.artifacts import ArtifactRepository

_CHUNK_SIZE = 1024 * 1024


def _sibling_temp(target, token: str):
    parent = os.path.dirname(target) or (b"." if isinstance(target, bytes) else ".")
    basename = os.path.basename(target)
    if isinstance(target, bytes):
        filename = b"." + basename + b".mote-" + token.encode("ascii") + b".tmp"
    else:
        filename = f".{basename}.mote-{token}.tmp"
    return os.path.join(parent, filename)


def _sibling_tombstone(target, transaction_id: str):
    parent = os.path.dirname(target) or (b"." if isinstance(target, bytes) else ".")
    basename = os.path.basename(target)
    transaction_key = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
    if isinstance(target, bytes):
        filename = b"." + basename + b".mote-delete-" + transaction_key.encode("ascii") + b".tombstone"
    else:
        filename = f".{basename}.mote-delete-{transaction_key}.tombstone"
    return os.path.join(parent, filename)


def _fsync_parent(target) -> None:
    parent = os.path.dirname(target) or (b"." if isinstance(target, bytes) else ".")
    fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AtomicPublisher:
    """Publishes one verified blob with same-directory replace semantics."""

    def __init__(self, repository: ArtifactRepository) -> None:
        if type(repository) is not ArtifactRepository:
            raise TypeError("atomic publisher requires an ArtifactRepository")
        self._repository = repository

    def replace_from_blob(
        self,
        target: PathLike,
        ref: ContentIdentity,
        *,
        metadata: ContentIdentity,
        expected: FileVersion,
    ) -> None:
        native = native_path(target)
        if metadata.size > MAX_METADATA_MANIFEST_BYTES:
            raise FilePublishError(
                "prepared metadata manifest exceeds the size limit",
                digest=metadata.digest,
            )
        if isinstance(expected, PresentVersion) and metadata.digest != expected.metadata_digest:
            raise FilePublishError(
                "prepared metadata does not match the expected file version",
                digest=metadata.digest,
            )
        preserved_metadata = decode_metadata_manifest(
            self._repository.read_bounded(
                metadata,
                maximum_bytes=MAX_METADATA_MANIFEST_BYTES,
            )
        )

        temp = None
        fd: Optional[int] = None
        try:
            for _ in range(32):
                candidate = _sibling_temp(native, f"{os.getpid()}-{secrets.token_hex(8)}")
                try:
                    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    temp = candidate
                    break
                except FileExistsError:
                    continue
            if fd is None or temp is None:
                raise FilePublishError(f"cannot allocate a unique temporary file for {os.fsdecode(native)}")

            with self._repository.open_verified(ref) as input_stream:
                while True:
                    chunk = input_stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]

            apply_metadata(fd, temp, preserved_metadata)
            os.fsync(fd)
            os.close(fd)
            fd = None
            self._verify_expected(native, expected)
            os.replace(temp, native)
            temp = None
            _fsync_parent(native)
        except FileOperationError:
            raise
        except OSError as exc:
            raise FilePublishError(
                f"cannot atomically publish {os.fsdecode(native)}",
                path=os.fsdecode(native),
                cause=exc,
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp is not None:
                try:
                    os.remove(temp)
                except FileNotFoundError:
                    pass

    def delete(
        self,
        target: PathLike,
        *,
        expected: PresentVersion,
        transaction_id: str,
    ):
        native = native_path(target)
        tombstone = _sibling_tombstone(native, transaction_id)
        try:
            self._verify_expected(native, expected)
            if os.path.lexists(tombstone):
                raise FilePublishError(
                    f"delete tombstone already exists for transaction {transaction_id}",
                    path=os.fsdecode(tombstone),
                    transaction_id=transaction_id,
                )
            os.replace(native, tombstone)
            _fsync_parent(native)
            return tombstone
        except FileOperationError:
            raise
        except OSError as exc:
            raise FilePublishError(
                f"cannot atomically delete {os.fsdecode(native)}",
                path=os.fsdecode(native),
                cause=exc,
            ) from exc

    def restore_deleted(
        self,
        target: PathLike,
        tombstone: PathLike,
        *,
        expected: PresentVersion,
    ) -> None:
        native = native_path(target)
        deleted = native_path(tombstone)
        try:
            if name_identity(native) != expected.name_identity or os.path.lexists(native):
                raise IdentityChangedError(
                    f"delete target changed before compensation: {os.fsdecode(native)}",
                    path=os.fsdecode(native),
                )
            fd = os.open(deleted, os.O_RDONLY)
            try:
                before = os.fstat(fd)
                first_digest = self._hash_fd(fd)
                second_digest = self._hash_fd(fd)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            metadata = capture_metadata(deleted, after)
            if (
                not same_open_file(before, after)
                or target_identity_from_stat(after) != expected.target_identity
                or after.st_size != expected.size
                or after.st_mtime_ns != expected.mtime_ns
                or first_digest != second_digest
                or first_digest != expected.digest
                or metadata.digest != expected.metadata_digest
            ):
                raise ContentChangedError(
                    f"delete tombstone changed before compensation: {os.fsdecode(deleted)}",
                    path=os.fsdecode(deleted),
                )
            os.replace(deleted, native)
            _fsync_parent(native)
        except FileOperationError:
            raise
        except OSError as exc:
            raise FilePublishError(
                f"cannot compensate deleted target {os.fsdecode(native)}",
                path=os.fsdecode(native),
                cause=exc,
            ) from exc

    @staticmethod
    def tombstone_for(target: PathLike, transaction_id: str):
        return _sibling_tombstone(native_path(target), transaction_id)

    @staticmethod
    def cleanup_tombstone(tombstone) -> None:
        try:
            os.remove(tombstone)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise FilePublishError(
                f"cannot remove delete tombstone {os.fsdecode(tombstone)}",
                path=os.fsdecode(tombstone),
                cause=exc,
            ) from exc
        _fsync_parent(tombstone)

    @staticmethod
    def _verify_expected(target, expected: FileVersion) -> None:
        if isinstance(expected, AbsentVersion):
            if name_identity(target) != expected.name_identity or os.path.lexists(target):
                raise IdentityChangedError(
                    f"target appeared before publication: {os.fsdecode(target)}",
                    path=os.fsdecode(target),
                )
            return

        if not isinstance(expected, PresentVersion):
            raise TypeError(f"unsupported file version: {type(expected).__name__}")
        try:
            entry = os.lstat(target)
        except FileNotFoundError as exc:
            raise IdentityChangedError(
                f"target disappeared before publication: {os.fsdecode(target)}",
                path=os.fsdecode(target),
                cause=exc,
            ) from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise UnsupportedFilesystemSemanticsError(
                f"cannot atomically replace this target: {os.fsdecode(target)}",
                path=os.fsdecode(target),
            )
        if getattr(entry, "st_nlink", 1) > 1:
            raise UnsupportedFilesystemSemanticsError(
                f"cannot atomically replace a file with multiple hard links: {os.fsdecode(target)}",
                path=os.fsdecode(target),
                link_count=entry.st_nlink,
            )
        try:
            fd = os.open(target, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise IdentityChangedError(
                f"target disappeared before publication: {os.fsdecode(target)}",
                path=os.fsdecode(target),
                cause=exc,
            ) from exc
        try:
            before = os.fstat(fd)
            first_digest = AtomicPublisher._hash_fd(fd)
            second_digest = AtomicPublisher._hash_fd(fd)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if not same_open_file(before, after):
            raise ContentChangedError(
                f"target changed during final verification: {os.fsdecode(target)}",
                path=os.fsdecode(target),
            )
        if (
            name_identity(target) != expected.name_identity
            or target_identity_from_stat(after) != expected.target_identity
        ):
            raise IdentityChangedError(
                f"target identity changed before publication: {os.fsdecode(target)}",
                path=os.fsdecode(target),
            )
        actual_metadata = capture_metadata(target, after)
        if (
            after.st_size != expected.size
            or after.st_mtime_ns != expected.mtime_ns
            or actual_metadata.digest != expected.metadata_digest
        ):
            raise StaleSnapshotError(
                f"target version changed before publication: {os.fsdecode(target)}",
                path=os.fsdecode(target),
            )
        if first_digest != second_digest or first_digest != expected.digest:
            raise ContentChangedError(
                f"target content changed before publication: {os.fsdecode(target)}",
                path=os.fsdecode(target),
            )

    @staticmethod
    def _hash_fd(fd: int) -> str:
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()


__all__ = ["AtomicPublisher"]
