#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FileWatcher — a dependency-free mtime/size polling file watcher.

Detects file creations, modifications, and deletions under a set of watched
roots by comparing successive ``(mtime_ns, size)`` snapshots — the same
dependency-free strategy already used by :class:`ConfigWatcher` and the cron
scheduler's hot-reload, rather than pulling in ``watchdog``/inotify.

The core is intentionally control-plane-agnostic, mirroring
:class:`~metagpt.environment.scheduling.scheduler.CronScheduler`: it just calls
an injected ``on_change(event)`` coroutine per detected change. The glue layer
(:mod:`service`) wires that to the hook system. The async loop lifecycle
(``start``/``stop``/``_loop``) is the same shape as the scheduler so behavior is
predictable across the two subsystems; tests can also drive :meth:`poll`
directly without starting the loop.
"""

from __future__ import annotations

import asyncio
import os
from fnmatch import fnmatch
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from metagpt.common.logs import log_class
from metagpt.environment.watching.events import (
    CREATED,
    DELETED,
    MODIFIED,
    FileChangeEvent,
)

#: A path's change-detection signature: ``(mtime_ns, size)``.
_Sig = Tuple[int, int]

OnChange = Callable[[FileChangeEvent], Awaitable[None]]


@log_class(level="DEBUG", exclude={"is_running"})
class FileWatcher:
    """Polls watched roots and emits a :class:`FileChangeEvent` per change.

    ``roots`` may be directories (walked recursively) or individual files.
    ``ignore`` is a list of ``fnmatch`` patterns tested against both the full
    path and each path component, so a pattern like ``"*.pyc"`` or ``".git"``
    prunes matching files/directories. ``on_change`` is awaited once per change.
    """

    def __init__(
        self,
        roots: Iterable[str],
        on_change: OnChange,
        *,
        ignore: Optional[Iterable[str]] = None,
        check_interval: float = 1.0,
    ):
        self._roots = [os.path.abspath(r) for r in roots]
        self._on_change = on_change
        self._ignore = list(ignore or [])
        self._check_interval = check_interval

        # Last-seen signature per absolute path (the diff baseline).
        self._state: Dict[str, _Sig] = {}
        self._primed = False
        self._stopped = True
        self._loop_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Establish the baseline (no events) and start the polling loop."""
        self._stopped = False
        self.prime()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the polling loop."""
        self._stopped = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def is_running(self) -> bool:
        return not self._stopped

    def prime(self) -> None:
        """Snapshot the current tree as the baseline without emitting changes."""
        self._state = self._snapshot()
        self._primed = True

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self.poll()
            except Exception:  # noqa: BLE001 — best-effort tick; keep watching
                pass
            await asyncio.sleep(self._check_interval)

    async def poll(self) -> List[FileChangeEvent]:
        """Take a fresh snapshot, diff it against the baseline, fire changes.

        The very first poll on an un-primed watcher establishes the baseline and
        emits ``created`` for everything currently present. Call :meth:`prime`
        (or :meth:`start`, which primes) first to suppress that initial burst.
        Returns the events fired this poll.
        """
        current = self._snapshot()
        events = self._diff(self._state, current)
        self._state = current
        self._primed = True
        for event in events:
            await self._on_change(event)
        return events

    # ------------------------------------------------------------------
    # Snapshot / diff (pure, sync — testable in isolation)
    # ------------------------------------------------------------------
    def _ignored(self, path: str) -> bool:
        if not self._ignore:
            return False
        name = os.path.basename(path)
        return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in self._ignore)

    def _snapshot(self) -> Dict[str, _Sig]:
        state: Dict[str, _Sig] = {}
        for root in self._roots:
            if os.path.isdir(root):
                self._walk_dir(root, state)
            elif os.path.isfile(root) and not self._ignored(root):
                self._record(root, state)
        return state

    def _walk_dir(self, root: str, state: Dict[str, _Sig]) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in place so os.walk skips them.
            dirnames[:] = [d for d in dirnames if not self._ignored(os.path.join(dirpath, d))]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                if not self._ignored(full):
                    self._record(full, state)

    @staticmethod
    def _record(path: str, state: Dict[str, _Sig]) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        state[path] = (st.st_mtime_ns, st.st_size)

    @staticmethod
    def _diff(old: Dict[str, _Sig], new: Dict[str, _Sig]) -> List[FileChangeEvent]:
        events: List[FileChangeEvent] = []
        for path, sig in new.items():
            prev = old.get(path)
            if prev is None:
                events.append(_event(path, CREATED, sig))
            elif prev != sig:
                events.append(_event(path, MODIFIED, sig))
        for path in old:
            if path not in new:
                events.append(FileChangeEvent(path=path, change_type=DELETED))
        return events


def _event(path: str, change_type: str, sig: _Sig) -> FileChangeEvent:
    mtime_ns, size = sig
    return FileChangeEvent(path=path, change_type=change_type, mtime=mtime_ns / 1e9, size=size)


__all__ = ["FileWatcher", "OnChange"]
