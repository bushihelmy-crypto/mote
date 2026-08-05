"""MessageActivity protocol — the new-message activity-signal slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MessageActivitySnapshot:
    generation: int
    pending: bool

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("message activity generation must be a non-negative integer")


class MessageActivity(Protocol):
    """The activity-signal slice collaborators await on the message buffer.

    Lets a waiter (the background pool's ``wait_any``, ``Role.wait_interruptible``)
    block until a new message arrives **without** reaching into the buffer's
    internal ``asyncio.Event``. Satisfied by ``MessageQueue``; the clear side of
    the signal stays owned by the queue (it clears on drain), so this face is
    wait-only.
    """

    def activity_snapshot(self) -> MessageActivitySnapshot: ...

    async def wait_for_activity(self, after_generation: int) -> MessageActivitySnapshot: ...


__all__ = ["MessageActivity", "MessageActivitySnapshot"]
