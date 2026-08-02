"""Background-owned deferred operation and terminal outcome contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable


class StopReason(str, Enum):
    USER_CANCEL = "user_cancel"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"


class StopDisposition(str, Enum):
    CHECKPOINT = "checkpoint"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class OperationSucceeded:
    output: Any


@dataclass(frozen=True, slots=True)
class OperationFailed:
    error: BaseException


@dataclass(frozen=True, slots=True)
class OperationCancelled:
    reason: str = "cancelled"


@dataclass(frozen=True, slots=True)
class OperationTimedOut:
    reason: str = "timed_out"


OperationOutcome = OperationSucceeded | OperationFailed | OperationCancelled | OperationTimedOut


@runtime_checkable
class DeferredOperation(Protocol):
    async def execute(self) -> OperationOutcome: ...

    async def request_stop(self, reason: StopReason, disposition: StopDisposition) -> OperationOutcome: ...

    async def aclose(self) -> None: ...


class CoroutineOperation:
    """Single-use adapter for ordinary coroutine factories."""

    def __init__(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        self._factory = factory
        self._task: asyncio.Task[Any] | None = None
        self._started = False
        self._closed = False
        self._outcome: OperationOutcome | None = None
        self._lock = asyncio.Lock()

    async def execute(self) -> OperationOutcome:
        async with self._lock:
            if self._outcome is not None:
                return self._outcome
            if self._started:
                raise RuntimeError("DeferredOperation instances are single-use")
            if self._closed:
                raise RuntimeError("DeferredOperation is closed")
            self._started = True
            self._task = asyncio.create_task(self._factory())
        try:
            self._outcome = OperationSucceeded(await self._task)
        except asyncio.CancelledError:
            self._outcome = OperationCancelled()
        except BaseException as exc:  # noqa: BLE001
            self._outcome = OperationFailed(exc)
        return self._outcome

    async def request_stop(self, reason: StopReason, disposition: StopDisposition) -> OperationOutcome:
        del disposition
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if reason is StopReason.TIMEOUT:
            self._outcome = OperationTimedOut()
        else:
            self._outcome = OperationCancelled(reason.value)
        return self._outcome

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "CoroutineOperation",
    "DeferredOperation",
    "OperationCancelled",
    "OperationFailed",
    "OperationOutcome",
    "OperationSucceeded",
    "OperationTimedOut",
    "StopDisposition",
    "StopReason",
]
