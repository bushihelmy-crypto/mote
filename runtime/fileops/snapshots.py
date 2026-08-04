"""Immutable, content-addressed sealed snapshots."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable, Optional

from mote.contracts.content.identity import ContentDigest, ContentIdentity
from mote.contracts.file.errors import ContentChangedError, IdentityChangedError, SnapshotDurabilityError
from mote.contracts.file.identity import EditableTextSnapshot, FileSnapshot, PathToken, PresentVersion, ProjectIdentity
from mote.runtime.fileops.encoding import decode_text, editable_text
from mote.runtime.fileops.identity import (
    PathLike,
    default_project_root,
    present_version,
    project_identity,
    resolve_existing_target,
    same_open_file,
    target_identity_from_stat,
)
from mote.runtime.fileops.metadata import capture_metadata
from mote.runtime.fileops.metadata_manifest import (
    MAX_METADATA_MANIFEST_BYTES,
    PreservedMetadata,
    encode_metadata_manifest,
)
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, FileMutationArtifactRepository

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ObservedFileVersion:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    version: PresentVersion
    metadata: PreservedMetadata


class SealedSnapshotReader:
    """Reads a regular file twice through one handle and seals the first pass."""

    def __init__(self, artifacts: FileMutationArtifactRepository) -> None:
        self.artifacts = artifacts

    def open_snapshot(
        self,
        path: PathLike,
        *,
        scope: ArtifactWriteScope,
        project_root: Optional[PathLike] = None,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
    ) -> FileSnapshot:
        observed, artifact = self._observe_stable(
            path,
            project_root=project_root,
            first_pass=lambda source_fd, maximum_bytes: scope.put_chunks(
                self._read_chunks(source_fd),
                maximum_bytes=maximum_bytes,
            ),
        )
        metadata_bytes = encode_metadata_manifest(observed.metadata)
        if len(metadata_bytes) > MAX_METADATA_MANIFEST_BYTES:
            raise SnapshotDurabilityError(
                "metadata manifest exceeds the snapshot budget",
                size=len(metadata_bytes),
                maximum=MAX_METADATA_MANIFEST_BYTES,
            )
        metadata_artifact = scope.put_bytes(metadata_bytes)
        decision = None
        if encoding is not None or fallback_encoding is not None:
            _, decision = decode_text(
                self.artifacts.read_bytes(artifact),
                explicit=encoding,
                fallback=fallback_encoding,
            )
        return FileSnapshot(
            requested_path=observed.requested_path,
            target_path=observed.target_path,
            project_identity=observed.project_identity,
            version=PresentVersion(
                name_identity=observed.version.name_identity,
                target_identity=observed.version.target_identity,
                size=observed.version.size,
                mtime_ns=observed.version.mtime_ns,
                digest=observed.version.digest,
                metadata_digest=metadata_artifact.digest,
            ),
            artifact=artifact,
            metadata=metadata_artifact,
            encoding=decision,
        )

    def probe(
        self,
        path: PathLike,
        *,
        project_root: Optional[PathLike] = None,
    ) -> ObservedFileVersion:
        observed, _ = self._observe_stable(
            path,
            project_root=project_root,
            first_pass=self._hash_first_pass,
        )
        return observed

    def _observe_stable(
        self,
        path: PathLike,
        *,
        project_root: Optional[PathLike],
        first_pass: Callable[[int, int], ContentIdentity],
    ) -> tuple[ObservedFileVersion, ContentIdentity]:
        requested, target = resolve_existing_target(path)
        source_fd = os.open(target.native, os.O_RDONLY)
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"not a regular file: {requested.display}")
            artifact = first_pass(source_fd, before.st_size)
            second_digest = self._hash_second_pass(source_fd)
            after = os.fstat(source_fd)
            live = os.stat(requested.native)
            metadata = capture_metadata(target, after)
            if not same_open_file(before, after):
                raise ContentChangedError(
                    f"{requested.display} changed while it was being read",
                    path=requested.display,
                )
            if target_identity_from_stat(live) != target_identity_from_stat(after):
                raise IdentityChangedError(
                    f"{requested.display} was replaced while it was being read",
                    path=requested.display,
                )
            if artifact.digest != second_digest or artifact.size != after.st_size:
                raise ContentChangedError(
                    f"{requested.display} changed while it was being read",
                    path=requested.display,
                )
            metadata_bytes = encode_metadata_manifest(metadata)
            root = default_project_root(target, project_root)
            return (
                ObservedFileVersion(
                    requested_path=requested,
                    target_path=target,
                    project_identity=project_identity(root),
                    version=present_version(
                        requested,
                        after,
                        digest=artifact.digest,
                        metadata_digest=hashlib.sha256(metadata_bytes).hexdigest(),
                    ),
                    metadata=metadata,
                ),
                artifact,
            )
        finally:
            os.close(source_fd)

    def read_text(
        self,
        snapshot: FileSnapshot,
        *,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
    ) -> tuple[str, EditableTextSnapshot]:
        raw = self.artifacts.read_bytes(snapshot.artifact)
        text, decision = decode_text(raw, explicit=encoding, fallback=fallback_encoding)
        return text, editable_text(raw, decision)

    @staticmethod
    def _read_chunks(source_fd: int) -> Iterator[bytes]:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, _CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    @classmethod
    def _hash_first_pass(cls, source_fd: int, maximum_bytes: int) -> ContentIdentity:
        digest = hashlib.sha256()
        size = 0
        for chunk in cls._read_chunks(source_fd):
            digest.update(chunk)
            size += len(chunk)
            if size > maximum_bytes:
                raise ContentChangedError(
                    "file grew while it was being probed",
                    expected_size=maximum_bytes,
                    actual_size=size,
                )
        return ContentIdentity(digest=ContentDigest(digest.hexdigest()), size=size)

    @staticmethod
    def _hash_second_pass(source_fd: int) -> str:
        os.lseek(source_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()


__all__ = ["ObservedFileVersion", "SealedSnapshotReader"]
