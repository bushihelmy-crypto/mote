"""Stable-worker dispatcher consuming the one shared admission queue."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mote.runtime.inference.bulkhead import BulkheadController, BulkheadIdentity, BulkheadPermit
from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueClosedError, QueueEntry

DispatchHandler = Callable[[QueueEntry, BulkheadPermit], Awaitable[None]]
DispatchTimeoutHandler = Callable[[QueueEntry], Awaitable[None]]
IdentityResolver = Callable[[QueueEntry], BulkheadIdentity]


class Dispatcher:
    def __init__(
        self,
        *,
        queue: FairAdmissionQueue,
        bulkheads: BulkheadController,
        identity_resolver: IdentityResolver,
        handler: DispatchHandler,
        timeout_handler: DispatchTimeoutHandler,
        worker_count: int,
    ) -> None:
        if worker_count <= 0:
            raise ValueError("dispatcher worker_count must be positive")
        self._queue = queue
        self._bulkheads = bulkheads
        self._identity_resolver = identity_resolver
        self._handler = handler
        self._timeout_handler = timeout_handler
        self._worker_count = worker_count
        self._workers: tuple[asyncio.Task[None], ...] = ()
        self._active = 0
        self._active_condition = asyncio.Condition()
        self._closing = False
        self._errors: list[Exception] = []

    @property
    def active(self) -> int:
        return self._active

    def start(self) -> None:
        if self._workers:
            raise RuntimeError("dispatcher already started")
        self._workers = tuple(
            asyncio.create_task(self._worker(), name=f"inference-dispatch-{index}")
            for index in range(self._worker_count)
        )

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
        if self._errors:
            errors, self._errors = self._errors, []
            raise ExceptionGroup("inference dispatch failures", errors)

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self._queue.close()
        if self._workers:
            await asyncio.gather(*self._workers)
        await self._bulkheads.close()

    async def _worker(self) -> None:
        while True:
            try:
                entry = await self._queue.dequeue()
            except QueueClosedError:
                return
            identity = self._identity_resolver(entry)
            try:
                permit = await self._bulkheads.acquire(identity, deadline=entry.deadline)
            except TimeoutError:
                try:
                    await self._timeout_handler(entry)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._errors.append(exc)
                await self._queue.task_done()
                continue
            await self._mark_active(1)
            try:
                await self._handler(entry, permit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors.append(exc)
            finally:
                await permit.release()
                await self._mark_active(-1)
                await self._queue.task_done()

    async def _mark_active(self, delta: int) -> None:
        async with self._active_condition:
            self._active += delta
            if self._active < 0:
                raise RuntimeError("dispatcher active count underflow")
            self._active_condition.notify_all()
