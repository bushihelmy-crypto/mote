"""RequestAssembler protocol — the request-building slice."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Union

if TYPE_CHECKING:
    from mote.contracts.schema import Message


class RequestAssembler(Protocol):
    """The request-building slice ``ContextProvider`` needs from the manager.

    ``ContextManager`` satisfies both this and ``MessageStore``; ContextProvider
    depends only on this face (it never stores/reads history directly).
    """

    async def prepare_request(
        self, user_prompt: Union[str, "Message", None] = None, *, manage: bool = True
    ) -> list["Message"]:
        """Build the request the think step sends: managed history + user prompt."""
        ...
