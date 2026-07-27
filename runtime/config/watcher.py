#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hot-reload watcher for the layered config.

Watches the on-disk config files that feed the layer stack and rebuilds the
typed :class:`Config` when any of them change, firing an ``on_reload`` callback
(chokidar achieves the same effect elsewhere). Implemented with plain mtime
polling so it adds no third-party dependency and stays trivially testable via
:meth:`ConfigWatcher.poll_once`.

Only file *content* changes, additions and deletions among the discovered
sources trigger a reload; env/cli/programmatic overrides are out of scope (they
are not file-backed). The watcher is entirely opt-in: nothing starts a thread
unless the application calls :meth:`start`.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from mote.runtime.config.loader import load_config
from mote.runtime.config.sources import discover_source_files

if TYPE_CHECKING:
    from mote.runtime.config.schema import Config

ReloadCallback = Callable[["Config"], None]


class ConfigWatcher:
    """Poll the config source files and reload :class:`Config` on change."""

    def __init__(
        self,
        cwd: Optional[Path] = None,
        *,
        profile: Optional[str] = None,
        interval: float = 1.0,
        on_reload: Optional[ReloadCallback] = None,
    ):
        self.cwd = Path(cwd) if cwd is not None else None
        self.profile = profile
        self.interval = interval
        self.on_reload = on_reload
        self._snapshot: Dict[str, float] = self._take_snapshot()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _take_snapshot(self) -> Dict[str, float]:
        """Map each existing source file path -> its current mtime."""
        snapshot: Dict[str, float] = {}
        for source_file in discover_source_files(self.cwd, profile=self.profile):
            try:
                snapshot[str(source_file.path)] = source_file.path.stat().st_mtime
            except OSError:
                continue
        return snapshot

    def _changed(self, current: Dict[str, float]) -> bool:
        return current != self._snapshot

    def poll_once(self) -> Optional["Config"]:
        """Check for changes once; reload + fire callback and return the new
        :class:`Config` if anything changed, else ``None``."""
        current = self._take_snapshot()
        if not self._changed(current):
            return None
        self._snapshot = current
        config = load_config(self.cwd, reload=True, profile=self.profile)
        if self.on_reload is not None:
            self.on_reload(config)
        return config

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def start(self) -> "ConfigWatcher":
        """Begin polling on a daemon thread (no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="config-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: Optional[float] = None) -> None:
        """Signal the polling thread to stop and join it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
