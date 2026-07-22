#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dependency-light file watching and hook integration.

Detect file creations/modifications/deletions under a set of roots by polling
``(mtime, size)`` (the same strategy as :class:`ConfigWatcher` and the cron
scheduler's hot-reload — no ``watchdog``/inotify dependency).

Layering: the core :class:`FileWatcher` is control-plane-agnostic (a pure
``on_change`` coroutine callback); :class:`FileWatchService` is the glue that
turns each change into a ``FileChanged`` hook fire via the ``HookManager``.
"""

from __future__ import annotations

from mote.common.watching.events import FileChangeEvent
from mote.common.watching.service import FileWatchService
from mote.common.watching.watcher import FileWatcher

__all__ = ["FileChangeEvent", "FileWatcher", "FileWatchService"]
