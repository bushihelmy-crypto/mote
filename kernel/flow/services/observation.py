"""Mailbox-to-history observation service for agent flows."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.constants.messages import INTERJECTION, MESSAGE_ROUTE_TO_ALL
from mote.contracts.schema import Message, MessagePriority
from mote.kernel.flow.context import FlowContext

_INTERJECTION_TEMPLATE = "The user sent a message while you were working:\n<user_query>\n{content}\n</user_query>"


class ObservationService:
    """Filter ingress messages and commit the selected view to history."""

    def __init__(self, *, context: Callable[[], FlowContext], memory) -> None:
        self._context = context
        self._memory = memory
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
        old_messages = [] if not ctx.enable_memory else self._memory.get()
        filtered = [
            message
            for message in news_raw
            if (message.cause_by in ctx.watch or ctx.name in message.send_to or MESSAGE_ROUTE_TO_ALL in message.send_to)
            and message not in old_messages
        ]
        committed = news_raw if ctx.observe_all else filtered
        if interjection:
            self.frame_interjections(committed)
        await self._memory.add_batch(committed)
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
