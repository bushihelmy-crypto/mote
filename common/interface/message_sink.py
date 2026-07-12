"""MessageSink protocol — the push (delivery) slice of the message buffer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from metagpt.common.schema import Message


@runtime_checkable
class MessageSink(Protocol):
    """The push slice a producer uses to deliver a message into the buffer.

    Lets a producer (the background pool's ``deliver`` choke point) hand a
    notification to the agent's inbox **without** depending on the concrete
    ``MessageQueue`` type or its drain/serialize side. Mirrors
    ``MessageActivity`` (the wait slice) and ``MessageStore`` (the store
    slice): each consumer sees only the face it needs. Satisfied by
    ``MessageQueue``.
    """

    def push(self, msg: "Message", priority: int = ...) -> None:
        """Append *msg* with the given priority (default NEXT)."""
        ...
