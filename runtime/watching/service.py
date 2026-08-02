"""File watcher integration with File Operations and typed Telemetry."""

from __future__ import annotations

import asyncio
from typing import Iterable, Optional, Protocol

from mote.contracts.events.file.observation import FileChangedEvent
from mote.contracts.ports.file.changes import FileChangePort
from mote.runtime.telemetry.logging import log_class
from mote.runtime.watching.watcher import FileWatcher

FILE_CHANGED_EVENT = "FileChanged"


class _TelemetryEmitter(Protocol):
    async def emit(self, event: FileChangedEvent) -> None: ...


@log_class(level="DEBUG")
class FileWatchService:
    """Fence external changes, then publish their exact version transition."""

    def __init__(
        self,
        roots: Iterable[str],
        *,
        file_changes: FileChangePort,
        telemetry: _TelemetryEmitter,
        ignore: Optional[Iterable[str]] = None,
        check_interval: float = 1.0,
    ) -> None:
        self._file_changes = file_changes
        self._telemetry = telemetry
        self._watcher = FileWatcher(
            roots,
            self._on_change,
            file_changes,
            ignore=ignore,
            check_interval=check_interval,
        )

    @property
    def watcher(self) -> FileWatcher:
        return self._watcher

    async def _on_change(self, event: FileChangedEvent) -> None:
        await asyncio.to_thread(
            self._file_changes.invalidate_external_change,
            event.path,
            prior=event.prior_version,
            current=event.version,
        )
        await self._telemetry.emit(event)

    async def start_async(self) -> None:
        await self._watcher.start_async()

    async def stop(self) -> None:
        await self._watcher.stop()


__all__ = ["FileWatchService", "FILE_CHANGED_EVENT"]
