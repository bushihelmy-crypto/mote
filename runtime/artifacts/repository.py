"""Generic verified content-addressed Artifact repository."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from mote.contracts.artifact import ArtifactContentRef, ContentLocator
from mote.contracts.content import ContentIdentity

_CHUNK_SIZE = 1_024 * 1_024


class ContentAddressedArtifactStore:
    """Filesystem CAS whose public API uses only ``ArtifactContentRef``."""

    def __init__(self, root: Path, *, hard_limit_bytes: int) -> None:
        self.root = Path(root)
        self.hard_limit_bytes = hard_limit_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        if type(content) is not bytes:
            raise TypeError("artifact content must be bytes")
        if len(content) > self.hard_limit_bytes:
            raise ValueError("artifact content exceeds the repository hard limit")
        digest = hashlib.sha256(content).hexdigest()
        ref = ArtifactContentRef(ContentIdentity(digest, len(content)), ContentLocator(f"sha256:{digest}"))
        path = self._path(ref)
        if path.exists():
            self.read_bytes(ref)
            return ref
        used_bytes = sum(item.identity.size for item in self.scan())
        if used_bytes + len(content) > self.hard_limit_bytes:
            raise ValueError("artifact repository hard limit would be exceeded")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
        return ref

    def adopt_file(self, source: Path, ref: ArtifactContentRef) -> None:
        """Verify and atomically adopt one staged file into the CAS."""
        self._validate(ref)
        digest = hashlib.sha256()
        size = 0
        with Path(source).open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != ref.identity.digest or size != ref.identity.size:
            raise ValueError("staged Artifact content does not match its reference")
        destination = self._path(ref)
        if destination.exists():
            self.read_bytes(ref)
            Path(source).unlink()
            return
        used_bytes = sum(item.identity.size for item in self.scan())
        if used_bytes + ref.identity.size > self.hard_limit_bytes:
            raise ValueError("artifact repository hard limit would be exceeded")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(source, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def payload_path(self, ref: ArtifactContentRef) -> Path:
        self._validate(ref)
        return self._path(ref)

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        self._validate(ref)
        content = self._path(ref).read_bytes()
        if len(content) != ref.identity.size or hashlib.sha256(content).hexdigest() != ref.identity.digest:
            raise ValueError("artifact content failed integrity verification")
        return content

    def scan(self) -> tuple[ArtifactContentRef, ...]:
        refs = []
        for path in self.root.glob("[0-9a-f][0-9a-f]/[0-9a-f]*"):
            digest = path.name
            if len(digest) != 64:
                continue
            refs.append(
                ArtifactContentRef(
                    ContentIdentity(digest, path.stat().st_size),
                    ContentLocator(f"sha256:{digest}"),
                )
            )
        return tuple(refs)

    def reclaim(self, ref: ArtifactContentRef) -> bool:
        self._validate(ref)
        path = self._path(ref)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def modified_time_ns(self, ref: ArtifactContentRef) -> int:
        self._validate(ref)
        return self._path(ref).stat().st_mtime_ns

    def _path(self, ref: ArtifactContentRef) -> Path:
        return self.root / ref.identity.digest[:2] / ref.identity.digest

    @staticmethod
    def _validate(ref: ArtifactContentRef) -> None:
        if not isinstance(ref, ArtifactContentRef) or ref.content_ref != f"sha256:{ref.identity.digest}":
            raise ValueError("artifact content reference is invalid for repository CAS")


__all__ = ["ContentAddressedArtifactStore"]
