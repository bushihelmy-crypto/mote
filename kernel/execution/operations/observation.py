"""Mailbox-to-history observation operation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from mote.contracts.conversation import Message, MessagePriority
from mote.contracts.conversation.fields import INTERJECTION
from mote.contracts.execution.models import MutationStatus
from mote.contracts.ports.execution.transaction import ExecutionTransactionPort
from mote.contracts.task.notification import is_background_task_notification
from mote.kernel.commands.contracts import HistoryProjection
from mote.kernel.execution.context import ExecutionContext

_INTERJECTION_TEMPLATE = "The user sent a message while you were working:\n<user_query>\n{content}\n</user_query>"


@dataclass(frozen=True, slots=True)
class ObservationResult:
    observed_count: int
    user_message_count: int
    background_notification_count: int


class ObservationService:
    """Commit admitted inbox messages to history."""

    def __init__(
        self,
        *,
        context: Callable[[], ExecutionContext],
        history_reader,
        transaction: ExecutionTransactionPort,
    ) -> None:
        self._context = context
        self._history_reader = history_reader
        self._transaction = transaction
        self._observation_index = 0
        self.latest_observed_message: Message | None = None

    async def observe(
        self,
        max_priority: int = MessagePriority.NEXT,
        *,
        interjection: bool = False,
    ) -> ObservationResult:
        ctx = self._context()
        if ctx.msg_buffer is None:
            return ObservationResult(0, 0, 0)
        lease = ctx.msg_buffer.reserve(max_priority=max_priority)
        news_raw = [item.message for item in lease.items]
        if not news_raw:
            ctx.msg_buffer.ack(lease)
            return ObservationResult(0, 0, 0)
        old_messages = [] if not ctx.enable_memory else self._history_reader.get()
        committed = [message for message in news_raw if message not in old_messages]
        if interjection:
            self.frame_interjections(committed)
        self._observation_index += 1
        fingerprint = hashlib.sha256("\n".join(message.id for message in committed).encode()).hexdigest()
        try:
            result = await self._transaction.record_history(
                self._transaction.context(f"observation:{self._observation_index}"),
                HistoryProjection(tuple(committed), fingerprint),
            )
        except BaseException:
            ctx.msg_buffer.release(lease)
            raise
        if result.status not in {
            MutationStatus.APPLIED,
            MutationStatus.ALREADY_APPLIED,
        }:
            ctx.msg_buffer.release(lease)
            raise RuntimeError(result.reason or result.status.value)
        ctx.msg_buffer.ack(lease)
        self.latest_observed_message = committed[-1] if committed else None
        background_count = sum(is_background_task_notification(message) for message in committed)
        user_count = sum(message.is_user_message() for message in committed) - background_count
        return ObservationResult(len(committed), user_count, background_count)

    @staticmethod
    def frame_interjections(messages: list[Message]) -> None:
        for message in messages:
            if not message.is_user_message() or message.metadata.get(INTERJECTION):
                continue
            message.content = _INTERJECTION_TEMPLATE.format(content=message.content)
            message.metadata[INTERJECTION] = True


__all__ = ["ObservationResult", "ObservationService"]
