#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FileWatcher — a dependency-free mtime/size polling file watcher.

Detects file creations, modifications, and deletions under a set of watched
roots by comparing successive ``(mtime_ns, size)`` snapshots — the same
dependency-free strategy already used by :class:`ConfigWatcher` and the cron
scheduler's hot-reload, rather than pulling in ``watchdog``/inotify.

The core is intentionally control-plane-agnostic, mirroring
:class:`~mote.environment.scheduling.scheduler.CronScheduler`: it just calls
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

from mote.common.logs import log_class
from mote.common.scheduling import PeriodicLoop
from mote.environment.watching.events import CREATED, DELETED, MODIFIED, FileChangeEvent

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
        # poll returns events; the loop ignores the value (Tick expects Optional[bool]).
        self._runner = PeriodicLoop(check_interval, self.poll, name="file-watcher")  # type: ignore[arg-type]

        # Last-seen signature per absolute path (the diff baseline).
        self._state: Dict[str, _Sig] = {}
        # Paths the agent itself just wrote, mapped to the on-disk signature at
        # write time (``None`` = the write deleted the file). The next poll
        # suppresses a detected change for such a path when its fresh signature
        # still matches, so our own edits aren't echoed back as external
        # changes. See :meth:`note_self_write` / :meth:`_suppress_self_writes`.
        self._self_writes: Dict[str, Optional[_Sig]] = {}
        self._primed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Establish the baseline (no events) and start the polling loop.

        Primes synchronously — fine for a small watched tree (tests, individual
        files). Production callers running inside the event loop should prefer
        :meth:`start_async`, whose initial walk is pushed off the loop so a large
        tree never blocks it.
        """
        self.prime()
        self._runner.start()

    async def start_async(self) -> None:
        """Async start: snapshot the baseline off the event loop, then run.

        The initial baseline is a recursive ``os.walk`` of every watched root.
        When the CLI is launched *outside* a git repo the project root defaults
        to the whole home directory, so that walk can take seconds and — if run
        on the event loop — freezes the UI outright. Pushing the snapshot to the
        default executor thread (the same off-loop pattern as the code-index
        cold scan) keeps the loop responsive while the baseline is built.
        """
        self._state = await asyncio.get_running_loop().run_in_executor(None, self._snapshot)
        self._primed = True
        self._runner.start()

    async def stop(self) -> None:
        """Cancel the polling loop."""
        await self._runner.stop()

    def is_running(self) -> bool:
        return self._runner.is_running()

    def prime(self) -> None:
        """Snapshot the current tree as the baseline without emitting changes."""
        self._state = self._snapshot()
        self._primed = True

    def note_self_write(self, path: str) -> None:
        """Record that *the agent itself* just wrote ``path``.

        Captures the current on-disk signature (or ``None`` when the path no
        longer exists, i.e. the write was a delete). The next :meth:`poll`
        suppresses a change for this path when the freshly observed signature
        still matches what we recorded here — so the watcher never echoes our
        own edit back as an external change. If the file diverges again before
        the next poll (an external change layered on top of ours), the
        signatures won't match and the change *is* reported. Cheap and
        best-effort: a failed stat just records ``None``.
        """
        abspath = os.path.abspath(path)
        try:
            st = os.stat(abspath)
            self._self_writes[abspath] = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._self_writes[abspath] = None

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def poll(self) -> List[FileChangeEvent]:
        """Take a fresh snapshot, diff it against the baseline, fire changes.

        The very first poll on an un-primed watcher establishes the baseline and
        emits ``created`` for everything currently present. Call :meth:`prime`
        (or :meth:`start`, which primes) first to suppress that initial burst.
        Returns the events fired this poll.
        """
        current = await asyncio.get_running_loop().run_in_executor(None, self._snapshot)
        events = self._diff(self._state, current)
        self._state = current
        self._primed = True
        if self._self_writes:
            events = self._suppress_self_writes(events, current)
        for event in events:
            await self._on_change(event)
        return events

    def _suppress_self_writes(self, events: List[FileChangeEvent], current: Dict[str, _Sig]) -> List[FileChangeEvent]:
        """Drop events for paths the agent just wrote (matching signatures).

        Compares against the fresh snapshot ``current`` rather than the event's
        (lossy float) mtime, and consumes (pops) each note so a later genuine
        change to the same path is reported normally.
        """
        kept: List[FileChangeEvent] = []
        for event in events:
            if event.path not in self._self_writes:
                kept.append(event)
                continue
            noted = self._self_writes.pop(event.path)
            # current.get(path) is None when the path is absent (deleted), which
            # also matches a recorded delete (noted is None).
            if current.get(event.path) != noted:
                kept.append(event)  # diverged since our write -> a real change
        return kept

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
