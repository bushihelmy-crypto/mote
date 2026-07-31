"""Owned subscription workers with retry, checkpoints, DLQ, and barriers."""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping

from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId
from mote.contracts.ports.events.subscription import (
    CommittedEventHandler,
    DeadLetterEntry,
    Reliability,
    SubscriptionCheckpoint,
    SubscriptionIdentity,
    SubscriptionSpec,
    SubscriptionStateStore,
)
from mote.runtime.events.mailbox import MailboxClosed, MailboxPutResult, MailboxSnapshot, SubscriptionMailbox


class SubscriptionState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    DEGRADED = "degraded"
    DRAINING = "draining"
    FAILED = "failed"
    CLOSED = "closed"


class SubscriptionFailed(RuntimeError):
    """A recoverable subscription cannot safely advance."""


@dataclass(frozen=True)
class SubscriptionSnapshot:
    identity: SubscriptionIdentity
    reliability: Reliability
    state: SubscriptionState
    failure: str | None
    mailbox: MailboxSnapshot
    acknowledged: tuple[tuple[StreamId, int], ...]
    persisted: tuple[tuple[StreamId, int], ...]


class SubscriptionWorker:
    """Single owner for one subscription mailbox and handler lifecycle."""

    def __init__(
        self,
        spec: SubscriptionSpec,
        handler: CommittedEventHandler,
        *,
        state_store: SubscriptionStateStore | None = None,
    ) -> None:
        if spec.reliability in {Reliability.DURABLE, Reliability.RELIABLE}:
            if state_store is None:
                raise ValueError("recoverable subscription requires a state store")
        self.spec = spec
        self._handler = handler
        self._state_store = state_store
        self._mailbox = SubscriptionMailbox(
            capacity=spec.capacity,
            overflow=spec.overflow,
        )
        self._state = SubscriptionState.NEW
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._acknowledged: dict[StreamId, int] = {}
        self._persisted: dict[StreamId, int] = {}
        self._since_checkpoint: dict[StreamId, int] = {}
        self._barrier = asyncio.Condition()

    @property
    def state(self) -> SubscriptionState:
        return self._state

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def mailbox(self) -> SubscriptionMailbox:
        return self._mailbox

    def snapshot(self) -> SubscriptionSnapshot:
        return SubscriptionSnapshot(
            identity=self.spec.identity,
            reliability=self.spec.reliability,
            state=self._state,
            failure=(None if self._failure is None else f"{type(self._failure).__name__}: {self._failure}"),
            mailbox=self._mailbox.snapshot(),
            acknowledged=tuple(sorted(self._acknowledged.items(), key=lambda item: str(item[0]))),
            persisted=tuple(sorted(self._persisted.items(), key=lambda item: str(item[0]))),
        )

    async def start(self) -> None:
        if self._state is not SubscriptionState.NEW:
            raise RuntimeError("subscription can only be started once")
        self._state = SubscriptionState.RUNNING
        self._task = asyncio.create_task(
            self._run(),
            name=f"event-subscription:{self.spec.identity}",
        )

    async def publish(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> MailboxPutResult | None:
        if self._state not in {
            SubscriptionState.RUNNING,
            SubscriptionState.DEGRADED,
        }:
            raise RuntimeError("subscription is not accepting events")
        event_filter = self.spec.event_filter
        if not event_filter.matches_stream(envelope.stream_id):
            return None
        if self.spec.reliability in {Reliability.LIVE, Reliability.LOSSY} and not (
            event_filter.matches_event_type(envelope.event_type)
        ):
            return None
        return await self._mailbox.put(envelope)

    async def checkpoint(self, stream_id: StreamId) -> int:
        if self._state not in {
            SubscriptionState.RUNNING,
            SubscriptionState.DEGRADED,
        }:
            raise RuntimeError("subscription checkpoint is unavailable")
        if not self.spec.event_filter.matches_stream(stream_id):
            return 0
        return await self._current_checkpoint(stream_id)

    async def wait_until(self, stream_id: StreamId, sequence: int) -> None:
        if type(sequence) is not int or sequence < 0:
            raise ValueError("barrier sequence is invalid")
        async with self._barrier:
            while self._acknowledged.get(stream_id, 0) < sequence:
                if self._state is SubscriptionState.FAILED:
                    raise SubscriptionFailed(
                        f"subscription {self.spec.identity} failed before barrier"
                    ) from self._failure
                if self._state is SubscriptionState.CLOSED:
                    raise SubscriptionFailed(f"subscription {self.spec.identity} closed before barrier")
                await self._barrier.wait()

    async def drain(self) -> None:
        if self._state is SubscriptionState.FAILED:
            raise SubscriptionFailed(f"subscription {self.spec.identity} cannot drain") from self._failure
        await self._mailbox.join()
        if self._state is SubscriptionState.FAILED:
            raise SubscriptionFailed(f"subscription {self.spec.identity} failed while draining") from self._failure
        await self._flush_checkpoints()

    async def aclose(self, *, drain: bool = True) -> None:
        if self._state is SubscriptionState.CLOSED:
            return
        if self._state is SubscriptionState.NEW:
            await self._mailbox.close()
            self._state = SubscriptionState.CLOSED
            return
        if drain:
            await self.drain()
        if self._state is not SubscriptionState.FAILED:
            self._state = SubscriptionState.DRAINING
        if drain:
            await self._mailbox.close()
        else:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            await self._mailbox.abort()
        if self._task is not None:
            if drain:
                await self._task
            self._task = None
        if self._state is not SubscriptionState.FAILED:
            self._state = SubscriptionState.CLOSED
        async with self._barrier:
            self._barrier.notify_all()

    async def _run(self) -> None:
        while True:
            try:
                envelope = await self._mailbox.get()
            except MailboxClosed:
                return
            if not await self._process(envelope):
                return

    async def _process(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> bool:
        if envelope.sequence <= await self._current_checkpoint(envelope.stream_id):
            await self._mailbox.task_done()
            return True
        if not self.spec.event_filter.matches_event_type(envelope.event_type):
            await self._acknowledge(envelope)
            await self._mailbox.task_done()
            return True
        first_failure_at: datetime | None = None
        last_error: BaseException | None = None
        for attempt in range(1, self.spec.retry.max_attempts + 1):
            try:
                async with asyncio.timeout(self.spec.retry.attempt_timeout_seconds):
                    await self._handler.handle(envelope)
                await self._acknowledge(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                first_failure_at = first_failure_at or datetime.now(timezone.utc)
                if attempt < self.spec.retry.max_attempts:
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
            else:
                await self._mailbox.task_done()
                return True
            break

        assert last_error is not None
        if self.spec.reliability is Reliability.RELIABLE:
            try:
                await self._quarantine(
                    envelope,
                    attempts=self.spec.retry.max_attempts,
                    error=last_error,
                    first_failure_at=first_failure_at or datetime.now(timezone.utc),
                )
            except Exception as exc:
                await self._mark_failed(exc)
                return False
            self._state = SubscriptionState.DEGRADED
            await self._mailbox.task_done()
            return True
        if self.spec.reliability in {Reliability.LIVE, Reliability.LOSSY}:
            self._state = SubscriptionState.DEGRADED
            await self._acknowledge(envelope)
            await self._mailbox.task_done()
            return True
        await self._mark_failed(last_error)
        return False

    async def _current_checkpoint(self, stream_id: StreamId) -> int:
        acknowledged = self._acknowledged.get(stream_id)
        if acknowledged is not None:
            return acknowledged
        persisted = 0
        if self._state_store is not None:
            persisted = await self._state_store.load(self.spec.identity, stream_id)
        self._persisted[stream_id] = persisted
        self._acknowledged[stream_id] = persisted
        self._since_checkpoint[stream_id] = 0
        async with self._barrier:
            self._barrier.notify_all()
        return persisted

    async def _acknowledge(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> None:
        stream_id = envelope.stream_id
        since_checkpoint = self._since_checkpoint.get(stream_id, 0) + 1
        if self._state_store is not None and since_checkpoint >= self.spec.checkpoint.persist_every:
            await self._save_checkpoint(stream_id, envelope.sequence)
            since_checkpoint = 0
        self._acknowledged[stream_id] = envelope.sequence
        self._since_checkpoint[stream_id] = since_checkpoint
        async with self._barrier:
            self._barrier.notify_all()

    async def _save_checkpoint(self, stream_id: StreamId, sequence: int) -> None:
        assert self._state_store is not None
        await self._state_store.save(
            SubscriptionCheckpoint(
                identity=self.spec.identity,
                stream_id=stream_id,
                sequence=sequence,
            )
        )
        self._persisted[stream_id] = sequence

    async def _flush_checkpoints(self) -> None:
        if self._state_store is None:
            return
        for stream_id, sequence in tuple(self._acknowledged.items()):
            if sequence > self._persisted.get(stream_id, 0):
                await self._save_checkpoint(stream_id, sequence)
                self._since_checkpoint[stream_id] = 0

    async def _quarantine(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
        *,
        attempts: int,
        error: BaseException,
        first_failure_at: datetime,
    ) -> None:
        assert self._state_store is not None
        checkpoint = SubscriptionCheckpoint(
            identity=self.spec.identity,
            stream_id=envelope.stream_id,
            sequence=envelope.sequence,
        )
        await self._state_store.quarantine(
            DeadLetterEntry(
                subscription=self.spec.identity,
                envelope=envelope,
                attempts=attempts,
                error=f"{type(error).__name__}: {error}",
                first_failed_at=first_failure_at,
                last_failed_at=datetime.now(timezone.utc),
            ),
            checkpoint,
        )
        self._acknowledged[envelope.stream_id] = envelope.sequence
        self._persisted[envelope.stream_id] = envelope.sequence
        self._since_checkpoint[envelope.stream_id] = 0
        async with self._barrier:
            self._barrier.notify_all()

    async def _mark_failed(self, error: BaseException) -> None:
        self._failure = error
        self._state = SubscriptionState.FAILED
        await self._mailbox.abort()
        async with self._barrier:
            self._barrier.notify_all()

    def _retry_delay(self, attempt: int) -> float:
        retry = self.spec.retry
        base = min(
            retry.maximum_delay_seconds,
            retry.initial_delay_seconds * (2 ** (attempt - 1)),
        )
        if not base or not retry.jitter_ratio:
            return base
        spread = base * retry.jitter_ratio
        return max(0.0, base + random.uniform(-spread, spread))


__all__ = [
    "SubscriptionFailed",
    "SubscriptionSnapshot",
    "SubscriptionState",
    "SubscriptionWorker",
]
