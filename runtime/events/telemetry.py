"""Bounded process-local telemetry fan-out with explicit task ownership."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, TypeVar, cast

from mote.contracts.ports.events.telemetry import (
    SyncTelemetryHandler,
    TelemetryHandler,
    TelemetryIdentity,
    TelemetryOverflow,
    TelemetrySubscriptionSpec,
)


class TelemetryState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    DEGRADED = "degraded"
    DRAINING = "draining"
    CLOSED = "closed"


class TelemetryPutResult(StrEnum):
    ENQUEUED = "enqueued"
    DROPPED = "dropped"
    COALESCED = "coalesced"


@dataclass(frozen=True)
class _ErasedTelemetryBinding:
    spec: TelemetrySubscriptionSpec
    handler: TelemetryHandler[object]
    sync_handler: SyncTelemetryHandler[object] | None = None
    event_type: type[object] | None = None


@dataclass(frozen=True)
class AllTelemetryBinding:
    """Explicit subscription to every observation event in one Runtime."""

    spec: TelemetrySubscriptionSpec
    handler: TelemetryHandler[object]
    sync_handler: SyncTelemetryHandler[object] | None = None

    def _erase(self) -> _ErasedTelemetryBinding:
        return _ErasedTelemetryBinding(self.spec, self.handler, self.sync_handler)


EventT = TypeVar("EventT")


@dataclass(frozen=True)
class _TypedTelemetryBinding(Generic[EventT]):
    spec: TelemetrySubscriptionSpec
    event_type: type[EventT]
    handler: TelemetryHandler[EventT]
    sync_handler: SyncTelemetryHandler[EventT] | None = None

    def erase(self) -> _ErasedTelemetryBinding:
        return _ErasedTelemetryBinding(
            self.spec,
            cast(TelemetryHandler[object], self.handler),
            cast(SyncTelemetryHandler[object], self.sync_handler),
            cast(type[object], self.event_type),
        )


@dataclass(frozen=True)
class TelemetryManifest:
    bindings: tuple[AllTelemetryBinding, ...]

    def __post_init__(self) -> None:
        identities = tuple(binding.spec.identity for binding in self.bindings)
        if len(set(identities)) != len(identities):
            raise ValueError("telemetry manifest contains duplicate identities")


@dataclass(frozen=True)
class TelemetryMailboxSnapshot:
    capacity: int
    depth: int
    unfinished: int
    dropped: int
    coalesced: int
    closed: bool


@dataclass(frozen=True)
class TelemetrySubscriptionSnapshot:
    identity: TelemetryIdentity
    state: TelemetryState
    failures: int
    last_failure: str | None
    mailbox: TelemetryMailboxSnapshot


@dataclass(frozen=True)
class _TelemetryItem:
    event: object
    synchronous: bool


class _TelemetryMailbox:
    def __init__(
        self,
        spec: TelemetrySubscriptionSpec,
        signal: Callable[[bool], None],
    ) -> None:
        self._spec = spec
        self._signal = signal
        self._items: deque[_TelemetryItem] = deque()
        self._unfinished = 0
        self._dropped = 0
        self._coalesced = 0
        self._closed = False
        self._lock = threading.Lock()

    def put(self, item: _TelemetryItem) -> TelemetryPutResult:
        with self._lock:
            if self._closed:
                return TelemetryPutResult.DROPPED
            was_idle = self._unfinished == 0
            if len(self._items) >= self._spec.capacity:
                if self._spec.overflow is TelemetryOverflow.DROP_NEWEST:
                    self._dropped += 1
                    return TelemetryPutResult.DROPPED
                if self._spec.overflow is TelemetryOverflow.DROP_OLDEST:
                    self._items.popleft()
                    self._unfinished -= 1
                    self._dropped += 1
                else:
                    key = _coalesce_key(item.event)
                    for index in range(len(self._items) - 1, -1, -1):
                        if _coalesce_key(self._items[index].event) == key:
                            self._items[index] = item
                            self._coalesced += 1
                            return TelemetryPutResult.COALESCED
                    self._items.popleft()
                    self._unfinished -= 1
                    self._dropped += 1
            self._items.append(item)
            self._unfinished += 1
        self._signal(was_idle)
        return TelemetryPutResult.ENQUEUED

    def pop(self) -> _TelemetryItem | None:
        with self._lock:
            return self._items.popleft() if self._items else None

    def task_done(self) -> bool:
        with self._lock:
            if self._unfinished < 1:
                raise ValueError("telemetry task_done called too many times")
            self._unfinished -= 1
            return self._unfinished == 0

    def close(self, *, discard: bool) -> bool:
        with self._lock:
            self._closed = True
            if discard:
                discarded = len(self._items)
                self._items.clear()
                self._unfinished -= discarded
            return self._unfinished == 0

    def snapshot(self) -> TelemetryMailboxSnapshot:
        with self._lock:
            return TelemetryMailboxSnapshot(
                capacity=self._spec.capacity,
                depth=len(self._items),
                unfinished=self._unfinished,
                dropped=self._dropped,
                coalesced=self._coalesced,
                closed=self._closed,
            )


class _TelemetryWorker:
    def __init__(self, binding: _ErasedTelemetryBinding) -> None:
        self.binding = binding
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._drained: asyncio.Event | None = None
        self._mailbox = _TelemetryMailbox(binding.spec, self._signal)
        self._task: asyncio.Task[None] | None = None
        self._state = TelemetryState.NEW
        self._failures = 0
        self._last_failure: str | None = None

    def start(self) -> None:
        if self._state is not TelemetryState.NEW:
            raise RuntimeError("telemetry subscription can only start once")
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._state = TelemetryState.RUNNING
        self._task = asyncio.create_task(
            self._run(),
            name=f"telemetry-subscription:{self.binding.spec.identity}",
        )

    def publish(self, event: object, *, synchronous: bool) -> TelemetryPutResult:
        if self._state not in {TelemetryState.RUNNING, TelemetryState.DEGRADED}:
            return TelemetryPutResult.DROPPED
        if self.binding.event_type is not None and type(event) is not self.binding.event_type:
            return TelemetryPutResult.DROPPED
        return self._mailbox.put(_TelemetryItem(event=event, synchronous=synchronous))

    async def drain(self) -> None:
        if self._drained is None:
            return
        await asyncio.sleep(0)
        await self._drained.wait()

    async def aclose(self, *, drain: bool) -> None:
        if self._state is TelemetryState.CLOSED:
            return
        if self._state is TelemetryState.NEW:
            self._mailbox.close(discard=True)
            self._state = TelemetryState.CLOSED
            return
        self._state = TelemetryState.DRAINING
        if drain:
            await self.drain()
        if self._mailbox.close(discard=not drain) and self._drained is not None:
            self._drained.set()
        if self._wake is not None:
            self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        close = getattr(self.binding.handler, "aclose", None)
        if close is not None:
            await close()
        self._state = TelemetryState.CLOSED

    def snapshot(self) -> TelemetrySubscriptionSnapshot:
        return TelemetrySubscriptionSnapshot(
            identity=self.binding.spec.identity,
            state=self._state,
            failures=self._failures,
            last_failure=self._last_failure,
            mailbox=self._mailbox.snapshot(),
        )

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            while True:
                item = self._mailbox.pop()
                if item is None:
                    self._wake.clear()
                    item = self._mailbox.pop()
                    if item is None:
                        break
                    self._wake.set()
                await self._handle(item)
                if self._mailbox.task_done() and self._drained is not None:
                    self._drained.set()
            if self._state is TelemetryState.DRAINING:
                return

    async def _handle(self, item: _TelemetryItem) -> None:
        try:
            if item.synchronous:
                if self.binding.sync_handler is not None:
                    self.binding.sync_handler.handle_sync(item.event)
            else:
                await self.binding.handler.handle(item.event)
        except Exception as exc:
            self._failures += 1
            self._last_failure = f"{type(exc).__name__}: {exc}"
            self._state = TelemetryState.DEGRADED

    def _signal(self, became_nonempty: bool) -> None:
        if not became_nonempty or self._loop is None:
            return

        def notify() -> None:
            if self._state not in {TelemetryState.RUNNING, TelemetryState.DEGRADED}:
                return
            if self._drained is not None:
                self._drained.clear()
            if self._wake is not None:
                self._wake.set()

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            notify()
        else:
            try:
                self._loop.call_soon_threadsafe(notify)
            except RuntimeError:
                return


class TelemetryHandle:
    def __init__(
        self,
        runtime: TelemetryRuntime,
        identity: TelemetryIdentity,
    ) -> None:
        self._runtime = runtime
        self.identity = identity
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._runtime.unsubscribe(self.identity)


class TelemetryRuntime:
    """Loss-tolerant observation plane; never participates in correctness."""

    def __init__(self, manifest: TelemetryManifest) -> None:
        self._workers = {binding.spec.identity: _TelemetryWorker(binding._erase()) for binding in manifest.bindings}
        self._state = TelemetryState.NEW

    @property
    def state(self) -> TelemetryState:
        return self._state

    def start(self) -> None:
        if self._state is not TelemetryState.NEW:
            raise RuntimeError("telemetry runtime can only start once")
        for worker in self._workers.values():
            worker.start()
        self._state = TelemetryState.RUNNING

    def emit_nowait(self, event: object, *, synchronous: bool = False) -> None:
        if self._state not in {TelemetryState.RUNNING, TelemetryState.DEGRADED}:
            return
        for worker in tuple(self._workers.values()):
            worker.publish(event, synchronous=synchronous)

    async def emit(self, event: object) -> None:
        self.emit_nowait(event)

    def emit_sync(self, event: object) -> None:
        self.emit_nowait(event, synchronous=True)

    async def _subscribe_erased(self, binding: _ErasedTelemetryBinding) -> TelemetryHandle:
        if self._state not in {TelemetryState.NEW, TelemetryState.RUNNING}:
            raise RuntimeError("telemetry runtime is not accepting subscriptions")
        identity = binding.spec.identity
        if identity in self._workers:
            raise ValueError(f"duplicate telemetry identity {identity!r}")
        worker = _TelemetryWorker(binding)
        if self._state is TelemetryState.RUNNING:
            worker.start()
        self._workers[identity] = worker
        return TelemetryHandle(self, identity)

    async def subscribe_typed(
        self,
        spec: TelemetrySubscriptionSpec,
        event_type: type[EventT],
        handler: TelemetryHandler[EventT],
        sync_handler: SyncTelemetryHandler[EventT] | None = None,
    ) -> TelemetryHandle:
        """Register one typed binding and erase it inside the Runtime boundary."""

        return await self._subscribe_erased(_TypedTelemetryBinding(spec, event_type, handler, sync_handler).erase())

    async def subscribe_all(
        self,
        spec: TelemetrySubscriptionSpec,
        handler: TelemetryHandler[object],
        sync_handler: SyncTelemetryHandler[object] | None = None,
    ) -> TelemetryHandle:
        """Register an observer that intentionally accepts every event."""

        return await self._subscribe_erased(AllTelemetryBinding(spec, handler, sync_handler)._erase())

    async def unsubscribe(self, identity: TelemetryIdentity) -> None:
        worker = self._workers.pop(identity, None)
        if worker is not None:
            await worker.aclose(drain=False)

    async def drain(self) -> None:
        for worker in tuple(self._workers.values()):
            await worker.drain()

    async def aclose(self) -> None:
        if self._state is TelemetryState.CLOSED:
            return
        self._state = TelemetryState.DRAINING
        for worker in tuple(self._workers.values()):
            await worker.aclose(drain=True)
        self._workers.clear()
        self._state = TelemetryState.CLOSED

    def snapshots(self) -> tuple[TelemetrySubscriptionSnapshot, ...]:
        return tuple(worker.snapshot() for worker in self._workers.values())


def _coalesce_key(event: object) -> tuple[type[object], object]:
    return type(event), getattr(event, "name", None)


__all__ = [
    "AllTelemetryBinding",
    "TelemetryHandle",
    "TelemetryMailboxSnapshot",
    "TelemetryManifest",
    "TelemetryPutResult",
    "TelemetryRuntime",
    "TelemetryState",
    "TelemetrySubscriptionSnapshot",
]
