"""LLMClient protocol — the LLM slice the think-engine and loop depend on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    # Type-only: `common.interface` is a low layer and must not import the higher
    # `router` layer at runtime. Guarded so the return annotation resolves for
    # the type checker without a real dependency.
    from mote.router.llm.llm_response import LLMResponse


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
    ) -> "LLMResponse":
        """Native tool-use call: returns text + structured tool calls."""
        ...
