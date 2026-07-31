"""Committed-envelope routing to independently owned subscription workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId
from mote.contracts.ports.events.journal import AppendResult, EventJournal, JournalIntegrityError
from mote.contracts.ports.events.subscription import (
    CommittedEventHandler,
    Reliability,
    SubscriptionIdentity,
    SubscriptionSpec,
    SubscriptionStateStore,
)
from mote.runtime.events.subscription import SubscriptionState, SubscriptionWorker


class DispatcherState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"
    CLOSED = "closed"


class DispatcherIntegrityError(RuntimeError):
    """Committed routing state is inconsistent with its journal."""


@dataclass(frozen=True)
class SubscriptionBinding:
    spec: SubscriptionSpec
    handler: CommittedEventHandler


@dataclass(frozen=True)
class SubscriptionManifest:
    bindings: tuple[SubscriptionBinding, ...]

    def __post_init__(self) -> None:
        identities = tuple(binding.spec.identity for binding in self.bindings)
        if len(set(identities)) != len(identities):
            raise ValueError("subscription manifest contains duplicate identities")


class CommittedEventDispatcher:
    """One process-local owner for committed fact fan-out and replay cursors."""

    def __init__(
        self,
        journal: EventJournal,
        manifest: SubscriptionManifest,
        *,
        state_store: SubscriptionStateStore | None = None,
    ) -> None:
        self._journal = journal
        self._manifest = manifest
        self._workers = tuple(
            SubscriptionWorker(
                binding.spec,
                binding.handler,
                state_store=state_store,
            )
            for binding in manifest.bindings
        )
        self._workers_by_identity = {worker.spec.identity: worker for worker in self._workers}
        self._streams: tuple[StreamId, ...] = ()
        self._cursors: dict[StreamId, int] = {}
        self._state = DispatcherState.NEW
        self._failure: BaseException | None = None

    @property
    def state(self) -> DispatcherState:
        return self._state

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def subscriptions(self) -> tuple[SubscriptionWorker, ...]:
        return self._workers

    @property
    def streams(self) -> tuple[StreamId, ...]:
        return self._streams

    def cursor(self, stream_id: StreamId) -> int:
        try:
            return self._cursors[stream_id]
        except KeyError as exc:
            raise ValueError(f"dispatcher does not own stream {stream_id!r}") from exc

    async def start(self, stream_ids: Sequence[StreamId]) -> None:
        if self._state is not DispatcherState.NEW:
            raise RuntimeError("dispatcher can only be started once")
        streams = tuple(stream_ids)
        if len(set(streams)) != len(streams):
            raise ValueError("dispatcher stream manifest contains duplicates")
        self._state = DispatcherState.STARTING
        try:
            reports = []
            for stream_id in streams:
                report = await self._journal.verify(stream_id)
                if not report.valid:
                    raise JournalIntegrityError(f"stream {stream_id!r} failed startup verification")
                reports.append(report)
            for worker in self._workers:
                await worker.start()
            for report in reports:
                await self._restore_stream(report.stream_id, report.current_version)
            self._streams = streams
            self._state = DispatcherState.RUNNING
        except BaseException as exc:
            self._failure = exc
            self._state = DispatcherState.FAILED
            await self._close_workers(drain=False)
            raise

    async def dispatch(self, result: AppendResult) -> None:
        if self._state is not DispatcherState.RUNNING:
            raise RuntimeError("dispatcher is not accepting committed facts")
        self._validate_append(result)
        try:
            await self._route(result.envelopes, self._workers)
        except BaseException as exc:
            self._failure = exc
            self._state = DispatcherState.FAILED
            raise
        self._cursors[result.stream_id] = result.current_version

    async def reconcile(self, stream_id: StreamId) -> int:
        if self._state is not DispatcherState.RUNNING:
            raise RuntimeError("dispatcher is not available for reconciliation")
        cursor = self.cursor(stream_id)
        report = await self._journal.verify(stream_id)
        if not report.valid or report.current_version < cursor:
            raise DispatcherIntegrityError(f"stream {stream_id!r} cannot reconcile from cursor {cursor}")
        async for envelope in self._journal.read(stream_id, after=cursor):
            if envelope.sequence != cursor + 1:
                raise DispatcherIntegrityError(f"stream {stream_id!r} reconciliation contains a sequence gap")
            await self._route((envelope,), self._workers)
            cursor = envelope.sequence
            self._cursors[stream_id] = cursor
        if cursor != report.current_version:
            raise DispatcherIntegrityError(f"stream {stream_id!r} changed across its verified snapshot")
        return cursor

    async def wait_until(
        self,
        subscription: SubscriptionIdentity,
        stream_id: StreamId,
        sequence: int,
    ) -> None:
        worker = self._worker(subscription)
        cursor = self.cursor(stream_id)
        if sequence > cursor:
            raise ValueError(f"barrier sequence {sequence} is beyond dispatched cursor {cursor}")
        if not worker.spec.event_filter.matches_stream(stream_id):
            raise ValueError("subscription does not track the requested stream")
        await worker.wait_until(stream_id, sequence)

    async def drain(self) -> None:
        for worker in self._recoverable_workers():
            await worker.drain()

    async def aclose(self, *, drain: bool = True) -> None:
        if self._state is DispatcherState.CLOSED:
            return
        if self._state is DispatcherState.NEW:
            await self._close_workers(drain=False)
            self._state = DispatcherState.CLOSED
            return
        if self._state is not DispatcherState.FAILED:
            self._state = DispatcherState.DRAINING
        errors = await self._close_workers(drain=drain)
        self._state = DispatcherState.CLOSED
        if errors:
            self._failure = errors[0]
            raise ExceptionGroup("subscription shutdown failed", errors)

    async def _restore_stream(
        self,
        stream_id: StreamId,
        current_version: int,
    ) -> None:
        workers = tuple(
            worker for worker in self._recoverable_workers() if worker.spec.event_filter.matches_stream(stream_id)
        )
        if not workers:
            self._cursors[stream_id] = current_version
            return
        checkpoints: list[int] = []
        for worker in workers:
            checkpoints.append(await worker.checkpoint(stream_id))
        if any(checkpoint > current_version for checkpoint in checkpoints):
            raise DispatcherIntegrityError(f"subscription checkpoint is ahead of stream {stream_id!r}")
        after = min(checkpoints)
        async for envelope in self._journal.read(stream_id, after=after):
            await self._route((envelope,), workers)
        for worker in workers:
            await worker.wait_until(stream_id, current_version)
        self._cursors[stream_id] = current_version

    async def _route(
        self,
        envelopes: Iterable[EventEnvelope[Mapping[str, JsonValue]]],
        workers: Sequence[SubscriptionWorker],
    ) -> None:
        ordered_workers = tuple(
            worker
            for reliability in (
                Reliability.LIVE,
                Reliability.LOSSY,
                Reliability.DURABLE,
                Reliability.RELIABLE,
            )
            for worker in workers
            if worker.spec.reliability is reliability
        )
        for envelope in envelopes:
            for worker in ordered_workers:
                await worker.publish(envelope)

    def _validate_append(self, result: AppendResult) -> None:
        cursor = self.cursor(result.stream_id)
        if result.previous_version != cursor:
            raise DispatcherIntegrityError(
                f"stream {result.stream_id!r} expected committed version {cursor}, "
                f"received {result.previous_version}"
            )
        if result.current_version != cursor + len(result.envelopes):
            raise DispatcherIntegrityError("append result version span is invalid")
        for offset, envelope in enumerate(result.envelopes, start=1):
            if envelope.stream_id != result.stream_id or envelope.sequence != cursor + offset:
                raise DispatcherIntegrityError("append result envelopes are not a contiguous committed span")

    def _worker(self, identity: SubscriptionIdentity) -> SubscriptionWorker:
        try:
            return self._workers_by_identity[identity]
        except KeyError as exc:
            raise ValueError(f"unknown subscription {identity!r}") from exc

    def _recoverable_workers(self) -> tuple[SubscriptionWorker, ...]:
        return tuple(
            worker for worker in self._workers if worker.spec.reliability in {Reliability.DURABLE, Reliability.RELIABLE}
        )

    async def _close_workers(self, *, drain: bool) -> list[Exception]:
        errors: list[Exception] = []
        for worker in self._workers:
            should_drain = (
                drain
                and worker.spec.reliability in {Reliability.DURABLE, Reliability.RELIABLE}
                and worker.state is not SubscriptionState.FAILED
            )
            try:
                await worker.aclose(drain=should_drain)
            except Exception as exc:
                errors.append(exc)
                if worker.state is not SubscriptionState.CLOSED:
                    try:
                        await worker.aclose(drain=False)
                    except Exception as close_exc:
                        errors.append(close_exc)
        return errors


__all__ = [
    "CommittedEventDispatcher",
    "DispatcherIntegrityError",
    "DispatcherState",
    "SubscriptionBinding",
    "SubscriptionManifest",
]
