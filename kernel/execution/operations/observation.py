"""Mailbox-to-history observation operation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from mote.contracts.conversation import Message, MessagePriority
from mote.contracts.conversation.fields import INTERJECTION, MESSAGE_ROUTE_TO_ALL
from mote.contracts.execution.models import MutationStatus
from mote.contracts.ports.execution.transaction import ExecutionTransactionPort
from mote.kernel.commands.contracts import HistoryProjection
from mote.kernel.execution.context import ExecutionContext

_INTERJECTION_TEMPLATE = "The user sent a message while you were working:\n<user_query>\n{content}\n</user_query>"


class ObservationService:
    """Filter ingress messages and commit the selected view to history."""

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
    ) -> int:
        ctx = self._context()
        if ctx.msg_buffer is None:
            return 0
        news_raw = ctx.msg_buffer.pop_all(max_priority=max_priority)
        if not news_raw:
            return 0
        old_messages = [] if not ctx.enable_memory else self._history_reader.get()
        filtered = [
            message
            for message in news_raw
            if (message.cause_by in ctx.watch or ctx.name in message.send_to or MESSAGE_ROUTE_TO_ALL in message.send_to)
            and message not in old_messages
        ]
        committed = news_raw if ctx.observe_all else filtered
        if interjection:
            self.frame_interjections(committed)
        self._observation_index += 1
        fingerprint = hashlib.sha256("\n".join(message.id for message in committed).encode()).hexdigest()
        result = await self._transaction.record_history(
            self._transaction.context(f"observation:{self._observation_index}"),
            HistoryProjection(tuple(committed), fingerprint),
        )
        if result.status not in {
            MutationStatus.APPLIED,
            MutationStatus.ALREADY_APPLIED,
        }:
            raise RuntimeError(result.reason or result.status.value)
        self.latest_observed_message = filtered[-1] if filtered else None
        return len(filtered)

    @staticmethod
    def frame_interjections(messages: list[Message]) -> None:
        for message in messages:
            if not message.is_user_message() or message.metadata.get(INTERJECTION):
                continue
            message.content = _INTERJECTION_TEMPLATE.format(content=message.content)
            message.metadata[INTERJECTION] = True


__all__ = ["ObservationService"]
