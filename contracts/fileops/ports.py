"""Narrow ports implemented by the runtime File Operations subsystem."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import ContextManager, Optional, Protocol, Sequence, Union, runtime_checkable

from mote.contracts.fileops.models import BlobRef, FileSnapshot, FileVersion, LockSpec

PathLike = Union[str, bytes, Path]


@runtime_checkable
class SnapshotReaderPort(Protocol):
    def open_snapshot(
        self,
        path: PathLike,
        *,
        project_root: Optional[PathLike] = None,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
    ) -> FileSnapshot:
        ...


@runtime_checkable
class ArtifactStorePort(Protocol):
    def read_bytes(self, ref: BlobRef) -> bytes:
        ...


@runtime_checkable
class LockManagerPort(Protocol):
    def acquire_many(
        self,
        specs: Sequence[LockSpec],
        *,
        timeout: Optional[float] = None,
        cancel: Optional[Event] = None,
    ) -> ContextManager[None]:
        ...


@runtime_checkable
class AtomicPublisherPort(Protocol):
    def replace_from_blob(
        self,
        target: PathLike,
        ref: BlobRef,
        *,
        metadata: BlobRef,
        expected: FileVersion,
    ) -> None:
        ...


__all__ = [
    "ArtifactStorePort",
    "AtomicPublisherPort",
    "LockManagerPort",
    "PathLike",
    "SnapshotReaderPort",
]
