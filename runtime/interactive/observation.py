"""Event-driven lifecycle for presentation attachments to live surfaces."""
from __future__ import annotations

import asyncio
from collections.abc import Callable


class SurfaceObservationHub:
    """Track observer tokens and wake them when a surface revision changes."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._sampling_task: asyncio.Task[None] | None = None

    def attach(self, token: str) -> None:
        if token in self._events:
            raise RuntimeError("surface observer token is already attached")
        self._events[token] = asyncio.Event()

    def start_sampling(
        self,
        advance_sequence: Callable[[], None],
        *,
        interval_seconds: float,
    ) -> None:
        """Publish periodic latest-value revisions while observers are attached."""
        if interval_seconds <= 0:
            raise ValueError("surface sampling interval must be positive")
        task = self._sampling_task
        if task is not None and not task.done():
            return
        self._sampling_task = asyncio.create_task(self._sample(advance_sequence, interval_seconds))

    def contains(self, token: str) -> bool:
        return token in self._events

    def notify(self) -> None:
        for event in self._events.values():
            event.set()

    async def wait_for_change(
        self,
        token: str,
        after_sequence: int,
        current_sequence: Callable[[], int],
    ) -> bool:
        event = self._events.get(token)
        if event is None:
            return False
        while token in self._events:
            if current_sequence() > after_sequence:
                return True
            event.clear()
            await event.wait()
        return False

    def detach(self, token: str) -> None:
        event = self._events.pop(token, None)
        if event is not None:
            event.set()
        if not self._events:
            self._stop_sampling()

    def close(self) -> None:
        events = tuple(self._events.values())
        self._events.clear()
        for event in events:
            event.set()
        self._stop_sampling()

    async def _sample(
        self,
        advance_sequence: Callable[[], None],
        interval_seconds: float,
    ) -> None:
        try:
            while self._events:
                await asyncio.sleep(interval_seconds)
                if not self._events:
                    return
                advance_sequence()
                self.notify()
        except asyncio.CancelledError:
            raise
        finally:
            if self._sampling_task is asyncio.current_task():
                self._sampling_task = None

    def _stop_sampling(self) -> None:
        task, self._sampling_task = self._sampling_task, None
        if task is not None:
            task.cancel()


__all__ = ["SurfaceObservationHub"]
