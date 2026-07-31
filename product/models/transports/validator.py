"""Bounded pre-commit response observation shared by provider adapters."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol


class _LineReader(Protocol):
    async def readline(self) -> bytes:
        ...


class PrecommitLimitExceeded(RuntimeError):
    pass


class PrecommitResponseGuard:
    def __init__(
        self,
        *,
        max_bytes: int,
        max_frames: int,
        max_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_bytes <= 0 or max_frames <= 0 or max_seconds <= 0:
            raise ValueError("precommit response limits must be positive")
        self._max_bytes = max_bytes
        self._max_frames = max_frames
        self._monotonic = monotonic
        self._deadline = monotonic() + max_seconds
        self._bytes = 0
        self._frames = 0
        self._committed = False

    async def readline(self, reader: _LineReader) -> bytes:
        if self._committed:
            return await reader.readline()
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise PrecommitLimitExceeded("response precommit time limit exceeded")
        try:
            async with asyncio.timeout(remaining):
                line = await reader.readline()
        except TimeoutError as exc:
            raise PrecommitLimitExceeded("response precommit time limit exceeded") from exc
        self.observe(len(line))
        return line

    def observe(self, size: int) -> None:
        if self._committed:
            return
        if size < 0:
            raise ValueError("observed response size cannot be negative")
        if self._monotonic() >= self._deadline:
            raise PrecommitLimitExceeded("response precommit time limit exceeded")
        self._bytes += size
        self._frames += 1
        if self._bytes > self._max_bytes:
            raise PrecommitLimitExceeded("response precommit byte limit exceeded")
        if self._frames > self._max_frames:
            raise PrecommitLimitExceeded("response precommit frame limit exceeded")

    def commit(self) -> None:
        self._committed = True


__all__ = ["PrecommitLimitExceeded", "PrecommitResponseGuard"]
