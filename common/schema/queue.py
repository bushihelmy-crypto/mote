#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Message queue and priority handling."""

from __future__ import annotations

import asyncio
import json
import time
from enum import Enum
from json import JSONDecodeError
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SerializeAsAny

from metagpt.common.schema.messages import Message
from metagpt.common.logs import logger


class MessagePriority(int, Enum):
    """Priority levels for messages in MessageQueue."""

    NOW = 0
    NEXT = 1
    LATER = 2


class QueuedMessage(BaseModel):
    """Priority envelope for a message stored in MessageQueue."""

    priority: MessagePriority = MessagePriority.NEXT
    message: SerializeAsAny[Message]


class MessageQueue(BaseModel):
    """Single-queue message buffer with priority-aware consumption."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _items: list[QueuedMessage] = PrivateAttr(default_factory=list)
    _new_msg_event: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    def pop(self, max_priority: int = MessagePriority.LATER) -> Message | None:
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

    def pop_all(self, max_priority: int = MessagePriority.LATER) -> List[Message]:
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
        if not self._items:
            self._new_msg_event.clear()
        return [item.message for item in drain]

    def push(self, msg: Message, priority: int = MessagePriority.NEXT):
        """Push a message with the given priority (default NEXT)."""
        self._items.append(QueuedMessage(priority=priority, message=msg))
        self._new_msg_event.set()

    def empty(self):
        """Return true if the queue is empty."""
        return len(self._items) == 0

    async def wait_for_message(self) -> None:
        """Block until a new message is pushed.

        Returns immediately when the pending-signal is already set (a message
        is waiting). The signal is cleared by ``pop_all`` when the queue drains,
        so this method only ever awaits — letting collaborators (the background
        pool, ``Role.wait_interruptible``) react to activity without reaching
        into the internal ``asyncio.Event``.
        """
        await self._new_msg_event.wait()

    async def dump(self) -> str:
        """Convert the ``MessageQueue`` object to a json string."""
        if self.empty():
            return "[]"
        return json.dumps(
            [{"priority": int(item.priority), "message": item.message.dump()} for item in self._items],
            ensure_ascii=False,
        )

    @staticmethod
    def load(data) -> "MessageQueue":
        """Convert the json string to the ``MessageQueue`` object."""
        queue = MessageQueue()
        try:
            lst = json.loads(data)
            for i in lst:
                if isinstance(i, dict) and "message" in i:
                    msg = Message.load(i["message"])
                    queue.push(msg, priority=i.get("priority", MessagePriority.NEXT))
                else:
                    queue.push(Message.load(i))
        except JSONDecodeError as e:
            logger.warning(f"JSON load failed: {data}, error:{e}")

        return queue


class LongTermMemoryItem(BaseModel):
    message: Message
    created_at: Optional[float] = Field(default_factory=time.time)

    def rag_key(self) -> str:
        return self.message.content
