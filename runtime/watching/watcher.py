"""Dependency-free polling watcher over exact File Operations versions."""

from __future__ import annotations

import asyncio
import os
import threading
from fnmatch import fnmatch
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

from mote.contracts.events.types import FileChangedEvent
from mote.contracts.fileops.models import (
    AbsentVersion,
    FileChangeAttribution,
    FileChangeKind,
    FileVersionTransition,
    PresentVersion,
)
from mote.contracts.ports import FileChangePort
from mote.runtime.logging import log_class
from mote.runtime.scheduling import PeriodicLoop

OnChange = Callable[[FileChangedEvent], Awaitable[None]]
_State = Dict[str, PresentVersion]


@log_class(
    level="DEBUG",
    exclude={
        "is_running",
        "poll",
        "_snapshot",
        "_walk_dir",
        "_record",
        "_ignored",
        "_diff",
    },
)
class FileWatcher:
    """Poll watched roots and report exact, externally attributed transitions."""

    def __init__(
        self,
        roots: Iterable[str],
        on_change: OnChange,
        file_changes: FileChangePort,
        *,
        ignore: Optional[Iterable[str]] = None,
        check_interval: float = 1.0,
    ):
        self._roots = [os.path.abspath(root) for root in roots]
        self._on_change = on_change
        self._file_changes = file_changes
        self._ignore = list(ignore or [])
        self._runner = PeriodicLoop(
            check_interval,
            self.poll,
            name="file-watcher",
        )  # type: ignore[arg-type]
        self._state: _State = {}
        self._state_lock = threading.Lock()
        self._poll_lock = asyncio.Lock()
        self._primed = False

    async def start_async(self) -> None:
        state = await self._snapshot_async()
        with self._state_lock:
            self._state = state
            self._primed = True
        self._runner.start()

    async def stop(self) -> None:
        await self._runner.stop()

    def is_running(self) -> bool:
        return self._runner.is_running()

    def prime(self) -> None:
        state = self._snapshot()
        with self._state_lock:
            self._state = state
            self._primed = True

    async def poll(self) -> List[FileChangedEvent]:
        async with self._poll_lock:
            current = await self._snapshot_async()
            with self._state_lock:
                prior_state = dict(self._state)
            events = await asyncio.to_thread(self._diff, prior_state, current)
            events = self._classify(events)
            for event in events:
                await self._on_change(event)
            with self._state_lock:
                self._state = current
                self._primed = True
            return events

    def _ignored(self, path: str) -> bool:
        if not self._ignore:
            return False
        name = os.path.basename(path)
        return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in self._ignore)

    def _snapshot(self) -> _State:
        state: _State = {}
        for root in self._roots:
            if os.path.isdir(root):
                self._walk_dir(root, state)
            elif os.path.isfile(root) and not self._ignored(root):
                self._record(root, state)
        return state

    async def _snapshot_async(self) -> _State:
        return await asyncio.to_thread(self._snapshot)

    def _walk_dir(self, root: str, state: _State) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not self._ignored(os.path.join(dirpath, name))]
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                if not self._ignored(full_path):
                    self._record(full_path, state)

    def _record(self, path: str, state: _State) -> None:
        try:
            version = self._file_changes.probe_file_version(path)
        except OSError:
            return
        if isinstance(version, PresentVersion):
            state[path] = version

    def _diff(self, old: _State, new: _State) -> List[FileChangedEvent]:
        events: List[FileChangedEvent] = []
        for path, version in new.items():
            prior = old.get(path)
            if prior is None:
                absent = AbsentVersion(version.name_identity)
                events.append(
                    FileChangedEvent(
                        path=path,
                        change_type=FileChangeKind.CREATED,
                        prior_version=absent,
                        version=version,
                    )
                )
            elif prior.name_identity != version.name_identity:
                events.extend(
                    (
                        FileChangedEvent(
                            path=path,
                            change_type=FileChangeKind.DELETED,
                            prior_version=prior,
                            version=AbsentVersion(prior.name_identity),
                        ),
                        FileChangedEvent(
                            path=path,
                            change_type=FileChangeKind.CREATED,
                            prior_version=AbsentVersion(version.name_identity),
                            version=version,
                        ),
                    )
                )
            elif prior != version:
                events.append(
                    FileChangedEvent(
                        path=path,
                        change_type=FileChangeKind.MODIFIED,
                        prior_version=prior,
                        version=version,
                    )
                )
        for path, prior in old.items():
            if path not in new:
                events.append(
                    FileChangedEvent(
                        path=path,
                        change_type=FileChangeKind.DELETED,
                        prior_version=prior,
                        version=AbsentVersion(prior.name_identity),
                    )
                )
        return events

    def _classify(
        self,
        events: List[FileChangedEvent],
    ) -> List[FileChangedEvent]:
        if not events:
            return events
        transitions = tuple(
            FileVersionTransition(
                path=event.path,
                prior=event.prior_version,
                current=event.version,
            )
            for event in events
        )
        classifications = self._file_changes.classify_transitions(transitions)
        if len(classifications) != len(events):
            raise ValueError("file transition classifications are incomplete")
        return [
            event
            for event, attribution in zip(events, classifications, strict=True)
            if attribution is FileChangeAttribution.EXTERNAL
        ]


__all__ = ["FileWatcher", "OnChange"]
