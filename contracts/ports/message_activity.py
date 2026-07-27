"""MessageActivity protocol — the new-message activity-signal slice."""

from __future__ import annotations

from typing import Protocol


class MessageActivity(Protocol):
    """The activity-signal slice collaborators await on the message buffer.

    Lets a waiter (the background pool's ``wait_any``, ``Role.wait_interruptible``)
    block until a new message arrives **without** reaching into the buffer's
    internal ``asyncio.Event``. Satisfied by ``MessageQueue``; the clear side of
    the signal stays owned by the queue (it clears on drain), so this face is
    wait-only.
    """

    async def wait_for_message(self) -> None:
        """Block until a new message is pushed (returns immediately if one is
        already pending)."""
        ...
