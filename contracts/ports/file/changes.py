"""Narrow File surface consumed by the file watcher."""

from __future__ import annotations

from typing import Optional, Protocol

from mote.contracts.file.identity import FileChangeAttribution, FileVersion, FileVersionTransition


class FileChangePort(Protocol):
    """Probe exact file versions and fence externally attributed changes."""

    def probe_file_version(
        self,
        path: str,
        *,
        prior: Optional[FileVersion] = None,
    ) -> FileVersion:
        ...

    def invalidate_external_change(
        self,
        path: str,
        *,
        prior: FileVersion,
        current: FileVersion,
    ) -> None:
        ...

    def classify_transitions(
        self,
        transitions: tuple[FileVersionTransition, ...],
    ) -> tuple[FileChangeAttribution, ...]:
        ...


__all__ = ["FileChangePort"]
