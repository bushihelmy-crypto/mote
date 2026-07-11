"""BaseThinkEngine — the think-side orchestration interface.

The Think counterpart to ``BaseToolExecutor`` on the Act side and ``BaseLoop``
on the cycle side. A think engine launches one LLM "think" round (XML text or
native tool-use), exposes its in-flight task state, and surfaces the single
``ThinkResult`` the channels/loop read. Making it an ABC lets the Role assemble
an alternative think strategy (e.g. one that reflects or retrieves before
asking) without the loop or channels knowing which concrete engine is in play.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from mote.common.schema import ThinkResult

if TYPE_CHECKING:
    from mote.common.interface import LLMClient, MessageStore


class BaseThinkEngine(ABC):
    """A replaceable think strategy.

    Contract used by the react loop and the command channels:
      - ``start(...)`` launches the think round (typically in the background).
      - ``result`` holds the latest ``ThinkResult`` (text + optional tool calls).
      - ``done`` reports whether the in-flight round has finished.
      - ``join()`` awaits the round and clears the task.
    """

    #: Collaborators every engine holds. Typed against the narrow Protocols so
    #: the assembly site is statically checked (any conforming LLM / store works).
    llm: "LLMClient"
    memory: "MessageStore"

    #: The single output contract for one think round, replaced wholesale by
    #: each round and read by callers through this attribute.
    result: ThinkResult

    @abstractmethod
    async def start(self, req, system_prompt, tool_specs=None, *, llm: "LLMClient") -> None:
        """Launch one think round.

        When ``tool_specs`` is provided the native tool-use channel is used;
        otherwise the XML text channel is used. ``llm`` is the per-request
        resolved client the loop passes after intelligent routing selects it.
        """

    @abstractmethod
    async def join(self) -> None:
        """Await the in-flight think round and clean up its task."""

    @property
    @abstractmethod
    def done(self) -> bool:
        """True when no round is pending or the pending round has finished."""
