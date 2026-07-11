#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PeriodicLoop — the cancellable asyncio loop shared by the polling subsystems.

The cron scheduler, file watcher, and background-task stall detector each ran a
near-identical ``start``/``stop``/``while-not-stopped`` scaffold. This primitive
captures exactly that scaffold so each subsystem only supplies its own ``tick``
body, keeping loop lifecycle behavior consistent across them.

It deliberately stays small — it knows nothing about cron, files, or tasks; the
business logic lives entirely in the injected ``tick`` callback.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Optional, Union

from mote.common.logs import logger

#: A tick callback. May be sync or async. Returning ``False`` stops the loop;
#: any other value (including ``None``) keeps it running.
Tick = Callable[[], Union[None, bool, Awaitable[Optional[bool]]]]


class PeriodicLoop:
    """Run ``tick`` every ``interval`` seconds until stopped.

    Behavior:

      * **best-effort** — an exception from ``tick`` is logged and swallowed so
        one bad tick never kills the loop. ``asyncio.CancelledError`` still
        propagates so cancellation works.
      * **self-stop** — a ``tick`` returning ``False`` ends the loop.
      * **ordering** — ``sleep_first`` controls whether the loop sleeps before
        the first tick (sleep-then-tick) or ticks immediately (tick-then-sleep).

    ``tick`` may be sync or async.
    """

    def __init__(
        self,
        interval: float,
        tick: Tick,
        *,
        name: Optional[str] = None,
        sleep_first: bool = False,
    ):
        self._interval = interval
        self._tick = tick
        self._name = name
        self._sleep_first = sleep_first
        self._stopped = True
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the loop (no-op if already running)."""
        if self.is_running():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name=self._name)

    def cancel(self) -> None:
        """Request cancellation without awaiting (sync, idempotent)."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()

    async def stop(self) -> None:
        """Cancel the loop and await its completion."""
        self.cancel()
        task, self._task = self._task, None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while not self._stopped:
            if self._sleep_first:
                await asyncio.sleep(self._interval)
                if self._stopped:
                    break
            try:
                result = self._tick()
                if inspect.isawaitable(result):
                    result = await result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — best-effort tick; keep looping
                logger.debug(f"PeriodicLoop[{self._name}] tick raised; continuing", exc_info=True)
                result = None
            if result is False:
                break
            if not self._sleep_first:
                await asyncio.sleep(self._interval)


__all__ = ["PeriodicLoop", "Tick"]
