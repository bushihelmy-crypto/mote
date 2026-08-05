#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Message queue and priority handling."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from mote.contracts.conversation.codec import dump_message, load_message
from mote.contracts.conversation.messages import Message
from mote.contracts.ports.conversation.message_activity import MessageActivitySnapshot


class MessagePriority(int, Enum):
    """Priority levels for messages in MessageQueue."""

    NOW = 0
    NEXT = 1
    LATER = 2


class QueuedMessage(BaseModel):
    """Priority envelope for a message stored in MessageQueue."""

    priority: MessagePriority = MessagePriority.NEXT
    message: Message


@dataclass(frozen=True, slots=True)
class InboxBatchLease:
    generation: int
    items: tuple[QueuedMessage, ...]


class MessageQueue(BaseModel):
    """Single-queue message buffer with priority-aware consumption."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _items: list[QueuedMessage] = PrivateAttr(default_factory=list)
    _activity_generation: int = PrivateAttr(default=0)
    _activity_condition: asyncio.Condition = PrivateAttr(default_factory=asyncio.Condition)
    _reserved: InboxBatchLease | None = PrivateAttr(default=None)

    def pop(self, max_priority: int = MessagePriority.NEXT) -> Message | None:
        """Pop the highest-priority message whose priority <= *max_priority*."""
        if not self._items:
            return None
        best_idx = -1
        best_pri = max_priority + 1
        for i in range(len(self._items)):
            pri = self._items[i].priority
            if pri <= max_priority and pri < best_pri:
                best_idx = i
                best_pri = pri
        if best_idx == -1:
            return None
        return self._items.pop(best_idx).message

    def pop_all(self, max_priority: int = MessagePriority.NEXT) -> List[Message]:
        """Pop messages with ``priority <= max_priority``, ordered by
        priority then insertion order."""
        keep, drain = [], []
        for item in self._items:
            if item.priority <= max_priority:
                drain.append(item)
            else:
                keep.append(item)
        drain.sort(key=lambda x: x.priority)
        self._items = keep
        return [item.message for item in drain]

    def reserve(self, max_priority: int = MessagePriority.NEXT) -> InboxBatchLease:
        if self._reserved is not None:
            raise RuntimeError("message queue already has an active drain lease")
        keep: list[QueuedMessage] = []
        drain: list[QueuedMessage] = []
        for item in self._items:
            (drain if item.priority <= max_priority else keep).append(item)
        drain.sort(key=lambda item: item.priority)
        self._items = keep
        lease = InboxBatchLease(self._activity_generation, tuple(drain))
        self._reserved = lease
        return lease

    def ack(self, lease: InboxBatchLease) -> None:
        self._require_lease(lease)
        self._reserved = None

    def release(self, lease: InboxBatchLease) -> None:
        self._require_lease(lease)
        self._items = list(lease.items) + self._items
        self._reserved = None

    def _require_lease(self, lease: InboxBatchLease) -> None:
        if self._reserved is not lease:
            raise RuntimeError("message queue drain lease is not current")

    def push(self, msg: Message, priority: MessagePriority = MessagePriority.NEXT) -> None:
        """Push a message with the given priority (default NEXT)."""
        self._items.append(QueuedMessage(priority=priority, message=msg))
        self._activity_generation += 1
        self._notify_activity()

    def empty(self) -> bool:
        """Return true if the queue is empty."""
        return len(self._items) == 0

    def activity_snapshot(self) -> MessageActivitySnapshot:
        return MessageActivitySnapshot(self._activity_generation, bool(self._items))

    async def wait_for_activity(self, after_generation: int) -> MessageActivitySnapshot:
        if type(after_generation) is not int or after_generation < 0:
            raise ValueError("after_generation must be a non-negative integer")
        async with self._activity_condition:
            await self._activity_condition.wait_for(
                lambda: self._activity_generation > after_generation or bool(self._items)
            )
            return self.activity_snapshot()

    def _notify_activity(self) -> None:
        async def notify() -> None:
            async with self._activity_condition:
                self._activity_condition.notify_all()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(notify())

    async def dump(self) -> str:
        """Convert the ``MessageQueue`` object to a json string."""
        if self.empty():
            return "[]"
        return json.dumps(
            [{"priority": int(item.priority), "message": dump_message(item.message)} for item in self._items],
            ensure_ascii=False,
        )

    @staticmethod
    def load(data: str) -> "MessageQueue":
        """Convert the json string to the ``MessageQueue`` object."""
        queue = MessageQueue()
        try:
            value = json.loads(data)
        except JSONDecodeError as exc:
            raise ValueError("message queue is not valid JSON") from exc
        if type(value) is not list:
            raise ValueError("message queue must be an array")
        for item in value:
            if type(item) is not dict or set(item) != {"priority", "message"}:
                raise ValueError("message queue item has an invalid shape")
            message = item["message"]
            priority = item["priority"]
            if type(message) is not str or type(priority) is not int:
                raise ValueError("message queue item primitives are invalid")
            queue.push(load_message(message), priority=MessagePriority(priority))

        return queue


class LongTermMemoryItem(BaseModel):
    message: Message
    created_at: Optional[float] = Field(default_factory=time.time)

    def rag_key(self) -> str:
        return self.message.content
