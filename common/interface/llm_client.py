"""LLMClient protocol — the LLM slice the think-engine and loop depend on."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class LLMClient(Protocol):
    """The LLM slice the think-engine and loop depend on.

    Satisfied by ``BaseLLM`` subclasses and test fakes. Keeps only the two call
    shapes plus the ``model`` name used for token math; the broad ``BaseLLM``
    surface (cost manager, compression, etc.) stays out of the contract.
    """

    model: str

    async def aask(
        self,
        msg: Any,
        system_msgs: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> str:
        """XML/text channel call: returns the assistant's plain text."""
        ...

    async def aask_tool(
        self,
        msg: Any,
        system_msgs: Optional[list[str]] = None,
        tools: Optional[list[dict]] = None,
        **kwargs: Any,
    ):
        """Native tool-use call: returns text + structured tool calls."""
        ...
