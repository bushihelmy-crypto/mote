#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FileWatchService — glue between the pure :class:`FileWatcher` and the hooks.

The watcher is intentionally control-plane-agnostic; this service owns the
wiring. Each detected change is turned into a ``FileChanged`` hook fire on the
injected :class:`~metagpt.common.interface.HookRunner` (normally a Role's
``HookManager``)::

    on_change(event) -> hook_runner.fire("FileChanged", {
        "path": event.path, "change_type": ..., "mtime": ..., "size": ...,
    })

``FileChanged`` matches on the ``path`` field (see ``HookManager._MATCH_FIELD``),
so a hook matcher like ``"*.py"`` or a regex selects which files to react to.
Mirrors :class:`CronService`: own the watcher, expose ``start``/``stop``, keep
the firing best-effort so a misbehaving hook never breaks the watch loop.

The service also doubles as an :class:`ObservationSubscriber` on the agent event
spine: when injected with the shared :class:`EventBus`, it subscribes itself and
turns each :class:`FileMutatedEvent` (a tool-driven write) into a
:meth:`FileWatcher.note_self_write`, so the watcher's next poll doesn't echo the
agent's own edit back as an external ``FileChanged``.
"""

from __future__ import annotations

from typing import Iterable, Optional

from metagpt.common.events import FileMutatedEvent
from metagpt.common.interface import HookRunner
from metagpt.common.logs import log_class, logger
from metagpt.environment.watching.events import FileChangeEvent
from metagpt.environment.watching.watcher import FileWatcher

#: Hook event name fired per detected change.
FILE_CHANGED_EVENT = "FileChanged"


@log_class(level="DEBUG")
class FileWatchService:
    """Owns a :class:`FileWatcher` and fires ``FileChanged`` hooks on changes."""

    # ObservationSubscriber priority: late (self-write notes are pure bookkeeping with
    # no influence to fold; they only need to land before the next poll tick).
    priority: int = 90

    def __init__(
        self,
        hook_runner: HookRunner,
        roots: Iterable[str],
        *,
        ignore: Optional[Iterable[str]] = None,
        check_interval: float = 1.0,
        bus: Optional["object"] = None,
    ):
        self._hooks = hook_runner
        self._watcher = FileWatcher(
            roots,
            self._on_change,
            ignore=ignore,
            check_interval=check_interval,
        )
        # Subscribe to the shared spine (if any) so tool-driven writes are
        # recorded as self-writes and don't echo back as external changes.
        self._bus = bus
        if bus is not None:
            bus.subscribe(self)

    @property
    def watcher(self) -> FileWatcher:
        return self._watcher

    # ------------------------------------------------------------------
    # ObservationSubscriber: record agent self-writes off the event spine
    # ------------------------------------------------------------------
    async def handle(self, event) -> None:
        """Note a tool-driven write so the watcher suppresses its own echo."""
        if isinstance(event, FileMutatedEvent) and event.path:
            self._watcher.note_self_write(event.path)
        return None

    # ------------------------------------------------------------------
    # Watcher callback
    # ------------------------------------------------------------------
    async def _on_change(self, event: FileChangeEvent) -> None:
        """Fire a ``FileChanged`` hook for one detected change (best-effort)."""
        payload = {
            "path": event.path,
            "change_type": event.change_type,
            "mtime": event.mtime,
            "size": event.size,
        }
        try:
            await self._hooks.fire(FILE_CHANGED_EVENT, payload)
        except Exception as exc:  # noqa: BLE001 — a hook failure must not break the watch loop
            logger.warning(f"FileWatchService: FileChanged hook for '{event.path}' failed: {exc}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._watcher.start()

    async def stop(self) -> None:
        if self._bus is not None:
            try:
                self._bus.unsubscribe(self)
            except Exception as exc:  # noqa: BLE001 — best-effort detach
                logger.debug(f"FileWatchService: bus detach on stop failed: {exc}")
        await self._watcher.stop()


__all__ = ["FileWatchService", "FILE_CHANGED_EVENT"]
