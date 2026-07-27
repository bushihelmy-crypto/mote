"""Bounded single-owner mailboxes for committed event subscriptions."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from mote.contracts.events import EventEnvelope, JsonValue
from mote.contracts.ports.event_subscription import OverflowPolicy


class MailboxClosed(RuntimeError):
    """The mailbox no longer accepts or yields events."""


class MailboxPutResult(StrEnum):
    ENQUEUED = "enqueued"
    DROPPED = "dropped"
    COALESCED = "coalesced"


@dataclass(frozen=True)
class MailboxSnapshot:
    capacity: int
    depth: int
    unfinished: int
    dropped: int
    coalesced: int
    closed: bool


class SubscriptionMailbox:
    """One bounded queue whose delivery behavior is fixed at construction."""

    def __init__(self, *, capacity: int, overflow: OverflowPolicy) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("mailbox capacity must be positive")
        self._capacity = capacity
        self._overflow = overflow
        self._items: deque[EventEnvelope[Mapping[str, JsonValue]]] = deque()
        self._unfinished = 0
        self._dropped = 0
        self._coalesced = 0
        self._closed = False
        self._condition = asyncio.Condition()

    async def put(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> MailboxPutResult:
        async with self._condition:
            self._raise_if_closed()
            if self._overflow is OverflowPolicy.BACKPRESSURE:
                while len(self._items) >= self._capacity and not self._closed:
                    await self._condition.wait()
                self._raise_if_closed()
                self._append(envelope)
                return MailboxPutResult.ENQUEUED
            if self._overflow is OverflowPolicy.DROP_NEWEST:
                if len(self._items) >= self._capacity:
                    self._dropped += 1
                    return MailboxPutResult.DROPPED
                self._append(envelope)
                return MailboxPutResult.ENQUEUED
            if self._overflow is OverflowPolicy.DROP_OLDEST:
                if len(self._items) >= self._capacity:
                    self._discard_oldest()
                self._append(envelope)
                return MailboxPutResult.ENQUEUED
            return self._coalesce(envelope)

    async def get(self) -> EventEnvelope[Mapping[str, JsonValue]]:
        async with self._condition:
            while not self._items and not self._closed:
                await self._condition.wait()
            if not self._items:
                raise MailboxClosed("mailbox is closed and drained")
            envelope = self._items.popleft()
            self._condition.notify_all()
            return envelope

    async def task_done(self) -> None:
        async with self._condition:
            if self._unfinished < 1:
                raise ValueError("mailbox task_done called too many times")
            self._unfinished -= 1
            if self._unfinished == 0:
                self._condition.notify_all()

    async def join(self) -> None:
        async with self._condition:
            while self._unfinished:
                await self._condition.wait()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def abort(self) -> None:
        async with self._condition:
            self._items.clear()
            self._unfinished = 0
            self._closed = True
            self._condition.notify_all()

    def snapshot(self) -> MailboxSnapshot:
        return MailboxSnapshot(
            capacity=self._capacity,
            depth=len(self._items),
            unfinished=self._unfinished,
            dropped=self._dropped,
            coalesced=self._coalesced,
            closed=self._closed,
        )

    def _append(self, envelope: EventEnvelope[Mapping[str, JsonValue]]) -> None:
        self._items.append(envelope)
        self._unfinished += 1
        self._condition.notify_all()

    def _discard_oldest(self) -> None:
        self._items.popleft()
        self._unfinished -= 1
        self._dropped += 1
        if self._unfinished == 0:
            self._condition.notify_all()

    def _coalesce(
        self,
        envelope: EventEnvelope[Mapping[str, JsonValue]],
    ) -> MailboxPutResult:
        key = envelope.stream_id, envelope.event_type
        for index in range(len(self._items) - 1, -1, -1):
            existing = self._items[index]
            if (existing.stream_id, existing.event_type) == key:
                self._items[index] = envelope
                self._coalesced += 1
                return MailboxPutResult.COALESCED
        if len(self._items) >= self._capacity:
            self._discard_oldest()
        self._append(envelope)
        return MailboxPutResult.ENQUEUED

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise MailboxClosed("mailbox is closed")


__all__ = [
    "MailboxClosed",
    "MailboxPutResult",
    "MailboxSnapshot",
    "SubscriptionMailbox",
]
