"""The data plane's sole wire in-flight capacity authority."""

from __future__ import annotations

import asyncio


class InFlightCapacityPermit:
    def __init__(self, owner: "InFlightCapacity") -> None:
        self._owner = owner
        self._released = False

    async def release(self) -> None:
        if self._released:
            raise RuntimeError("in-flight capacity permit already released")
        self._released = True
        await self._owner._release()

    async def __aenter__(self) -> "InFlightCapacityPermit":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


class InFlightCapacity:
    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("in-flight limit must be positive")
        self._limit = limit
        self._condition = asyncio.Condition()
        self._in_flight = 0
        self._closed = False

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self, *, deadline: float) -> InFlightCapacityPermit:
        async with self._condition:
            loop = asyncio.get_running_loop()
            while self._in_flight >= self._limit:
                if self._closed:
                    raise RuntimeError("in-flight capacity is closed")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("in-flight capacity deadline exceeded")
                await asyncio.wait_for(self._condition.wait(), timeout=remaining)
            if self._closed:
                raise RuntimeError("in-flight capacity is closed")
            self._in_flight += 1
            return InFlightCapacityPermit(self)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def _release(self) -> None:
        async with self._condition:
            if self._in_flight <= 0:
                raise RuntimeError("in-flight capacity underflow")
            self._in_flight -= 1
            self._condition.notify(1)
