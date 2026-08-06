"""Composition root and lifecycle owner for the process-local Event Fabric."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from typing import Callable, Sequence

from mote.contracts.events.envelope import StreamId
from mote.contracts.ports.events.journal import (
    AppendResult,
    EventJournal,
    EventJournalError,
    JournalIntegrityError,
    StreamVersionConflict,
    StreamWriterFence,
    UncommittedFact,
)
from mote.contracts.ports.events.subscription import ManagedSubscriptionStateStore
from mote.runtime.events.dispatcher import CommittedEventDispatcher, SubscriptionManifest
from mote.runtime.events.health import FabricHealth, FabricHealthSnapshot, FabricHealthState, FabricState
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.telemetry.logging import log_class


class EventFabricUnavailable(RuntimeError):
    """The fabric cannot currently guarantee recoverable mutation semantics."""


class EventFabricReadOnly(EventFabricUnavailable):
    """The journal is readable but recoverable writes are disabled."""


class CommittedDispatchError(EventFabricUnavailable):
    """A fact committed durably but could not be routed in this process."""

    def __init__(self, result: AppendResult, cause: BaseException) -> None:
        self.result = result
        self.cause = cause
        super().__init__(
            f"facts through {result.stream_id!r}/{result.current_version} committed "
            "but dispatch failed; journal replay is required"
        )


@log_class(level="DEBUG", exclude={"health_snapshot"})
class EventFabric:
    """Single admission point for journal commit, routing, and shutdown."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        streams: Sequence[StreamId],
        subscriptions: SubscriptionManifest,
        state_store: ManagedSubscriptionStateStore | None = None,
        health: FabricHealth | None = None,
        telemetry: TelemetryRuntime | None = None,
        on_commit: Callable[[AppendResult], None] | None = None,
    ) -> None:
        stream_manifest = tuple(streams)
        if len(set(stream_manifest)) != len(stream_manifest):
            raise ValueError("event fabric stream manifest contains duplicates")
        self._journal = journal
        self._streams = stream_manifest
        self._state_store = state_store
        self._health = health or FabricHealth()
        self._telemetry = telemetry
        self._on_commit = on_commit
        self._dispatcher = CommittedEventDispatcher(
            journal,
            subscriptions,
            state_store=state_store,
        )
        self._state = FabricState.NEW
        self._append_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[AppendResult]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._owner_thread_id: int | None = None

    @property
    def state(self) -> FabricState:
        return self._state

    @property
    def dispatcher(self) -> CommittedEventDispatcher:
        return self._dispatcher

    def health_snapshot(self) -> FabricHealthSnapshot:
        return self._health.snapshot(
            self._state,
            self._dispatcher,
            self._telemetry,
        )

    async def start(self) -> None:
        if self._state is not FabricState.NEW:
            raise RuntimeError("event fabric can only be started once")
        self._owner_loop = asyncio.get_running_loop()
        self._owner_thread_id = threading.get_ident()
        self._state = FabricState.STARTING
        state_store = self._state_store
        state_store_open = False
        try:
            if state_store is not None:
                await state_store.aopen()
                state_store_open = True
            await self._dispatcher.start(self._streams)
        except BaseException as exc:
            self._state = FabricState.FAILED
            self._health.mark_unavailable(
                "fabric.startup",
                f"{type(exc).__name__}: {exc}",
            )
            if state_store_open and state_store is not None:
                with suppress(Exception):
                    await state_store.aclose()
            raise
        self._health.clear("fabric.startup")
        self._state = FabricState.RUNNING

    async def append(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
    ) -> AppendResult:
        self._assert_write_ready()
        task = asyncio.create_task(
            self._append_committed(
                stream_id,
                facts,
            ),
            name=f"event-fabric-append:{stream_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise

    async def append_guarded(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        """Append a domain CAS exactly once; conflicts are never reconciled and retried."""

        self._assert_write_ready()
        task = asyncio.create_task(
            self._append_guarded_committed(
                stream_id,
                facts,
                expected_version=expected_version,
                writer=writer,
            ),
            name=f"event-fabric-guarded-append:{stream_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise

    def append_from_thread(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
    ) -> AppendResult:
        """Commit from a synchronous domain running outside the owner loop."""

        owner_loop = self._owner_loop
        if self._state is not FabricState.RUNNING or owner_loop is None:
            raise EventFabricUnavailable("event fabric is not running")
        if threading.get_ident() == self._owner_thread_id:
            raise RuntimeError("append_from_thread must be called from a non-owner thread")
        if owner_loop.is_closed() or not owner_loop.is_running():
            raise EventFabricUnavailable("event fabric owner loop is unavailable")
        append_coro = self.append(stream_id, tuple(facts))
        try:
            future = asyncio.run_coroutine_threadsafe(append_coro, owner_loop)
        except RuntimeError as exc:
            append_coro.close()
            raise EventFabricUnavailable("event fabric owner loop is unavailable") from exc
        return future.result()

    async def wait_until(
        self,
        subscription,
        stream_id: StreamId,
        sequence: int,
    ) -> None:
        await self._dispatcher.wait_until(subscription, stream_id, sequence)

    async def aclose(self) -> None:
        if self._state is FabricState.CLOSED:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_owned(),
                name="event-fabric-close",
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise

    async def _append_committed(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
    ) -> AppendResult:
        async with self._append_lock:
            try:
                expected_version = self._dispatcher.cursor(stream_id)
                try:
                    result = await self._journal.append(
                        stream_id,
                        facts,
                        expected_version=expected_version,
                    )
                except StreamVersionConflict as conflict:
                    if conflict.actual <= expected_version:
                        raise
                    await self._dispatcher.reconcile(stream_id)
                    result = await self._journal.append(
                        stream_id,
                        facts,
                        expected_version=self._dispatcher.cursor(stream_id),
                    )
            except JournalIntegrityError as exc:
                self._health.mark_unavailable(
                    "fabric.journal",
                    f"{type(exc).__name__}: {exc}",
                )
                self._state = FabricState.FAILED
                raise
            except (EventJournalError, OSError) as exc:
                self._health.mark_read_only(
                    "fabric.journal",
                    f"{type(exc).__name__}: {exc}",
                )
                raise EventFabricReadOnly("event journal is not writable") from exc
            try:
                if self._on_commit is not None:
                    self._on_commit(result)
                await self._dispatcher.dispatch(result)
            except BaseException as exc:
                self._health.mark_unavailable(
                    "fabric.dispatch",
                    f"{type(exc).__name__}: {exc}",
                )
                self._state = FabricState.FAILED
                raise CommittedDispatchError(result, exc) from exc
            self._health.clear("fabric.journal")
            self._health.clear("fabric.dispatch")
            return result

    async def _append_guarded_committed(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        async with self._append_lock:
            append_guarded = getattr(self._journal, "append_guarded", None)
            if append_guarded is None:
                raise EventFabricUnavailable("event journal does not support guarded appends")
            try:
                result = await append_guarded(
                    stream_id,
                    facts,
                    expected_version=expected_version,
                    writer=writer,
                )
            except StreamVersionConflict:
                raise
            except JournalIntegrityError as exc:
                self._health.mark_unavailable(
                    "fabric.journal",
                    f"{type(exc).__name__}: {exc}",
                )
                self._state = FabricState.FAILED
                raise
            except (EventJournalError, OSError) as exc:
                self._health.mark_read_only(
                    "fabric.journal",
                    f"{type(exc).__name__}: {exc}",
                )
                raise EventFabricReadOnly("event journal is not writable") from exc
            try:
                if self._on_commit is not None:
                    self._on_commit(result)
                await self._dispatcher.dispatch(result)
            except BaseException as exc:
                self._health.mark_unavailable(
                    "fabric.dispatch",
                    f"{type(exc).__name__}: {exc}",
                )
                self._state = FabricState.FAILED
                raise CommittedDispatchError(result, exc) from exc
            self._health.clear("fabric.journal")
            self._health.clear("fabric.dispatch")
            return result

    async def _close_owned(self) -> None:
        if self._state is FabricState.NEW:
            await self._dispatcher.aclose(drain=False)
            self._state = FabricState.CLOSED
            return
        if self._state is not FabricState.FAILED:
            self._state = FabricState.DRAINING
        errors: list[Exception] = []
        if self._inflight:
            results = await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, Exception))
        try:
            await self._dispatcher.aclose(drain=True)
        except Exception as exc:
            errors.append(exc)
            with suppress(Exception):
                await self._dispatcher.aclose(drain=False)
        if self._state_store is not None:
            try:
                await self._state_store.aclose()
            except Exception as exc:
                errors.append(exc)
        self._state = FabricState.CLOSED
        if errors:
            raise ExceptionGroup("event fabric shutdown failed", errors)

    def _assert_write_ready(self) -> None:
        if self._state is not FabricState.RUNNING:
            raise EventFabricUnavailable("event fabric is not running")
        snapshot = self.health_snapshot()
        if snapshot.state is FabricHealthState.READ_ONLY:
            raise EventFabricReadOnly("event fabric is read-only")
        if not snapshot.writable:
            raise EventFabricUnavailable("event fabric is unavailable")


__all__ = [
    "CommittedDispatchError",
    "EventFabric",
    "EventFabricReadOnly",
    "EventFabricUnavailable",
]
