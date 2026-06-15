"""MessageStore protocol — the conversation-store slice."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from metagpt.common.schema import Message


@runtime_checkable
class MessageStore(Protocol):
    """The conversation-store slice the loop / channels / think-engine use.

    Implemented by ``ContextManager`` (production) and any test double. Only the
    four methods actually called downstream are part of the contract; the
    orchestration side of ``ContextManager`` (``manage_history`` etc.) is
    deliberately NOT here, so a consumer typed against ``MessageStore`` cannot
    reach it.
    """

    def get(self, k: int = 0) -> list["Message"]:
        """Return the most-recent ``k`` messages (``k<=0`` -> all)."""
        ...

    async def add(self, message: "Message") -> None:
        """Append one message to the stored history (emits MessageAppendedEvent)."""
        ...

    async def add_batch(self, messages: list["Message"]) -> None:
        """Append several messages, skipping falsy entries."""
        ...

    def delete(self, message: "Message") -> None:
        """Remove a message if present (no-op when absent)."""
        ...
