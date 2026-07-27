"""Think-side execution interface.

The Think counterpart to ``BaseToolExecutor`` on the Act side and the flow engine
on the cycle side. A think engine launches one LLM "think" round (XML text or
native tool-use), exposes its in-flight task state, and surfaces the single
``ThinkResult`` the channels/loop read. Making it an ABC lets the Role assemble
an alternative think strategy (e.g. one that reflects or retrieves before
asking) without the loop or channels knowing which concrete engine is in play.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from mote.contracts.think import ThinkResult

if TYPE_CHECKING:
    from mote.contracts.ports import MessageStore, ModelRoute


class BaseThinkEngine(ABC):
    """A replaceable think strategy.

    Contract used by the react loop and the command channels:
      - ``start(...)`` launches the think round (typically in the background).
      - ``reinstate(...)`` primes a result recovered from the durable journal,
        skipping the LLM (the durable resume path — closes the re-pay window).
      - ``result`` holds the latest ``ThinkResult`` (text + optional tool calls).
      - ``done`` reports whether the in-flight round has finished.
      - ``join()`` awaits the round and clears the task.
    """

    #: Collaborators every engine holds. Typed against the narrow Protocols so
    #: the assembly site is statically checked (any conforming LLM / store works).
    model_route: "ModelRoute"
    memory: "MessageStore"

    #: The single output contract for one think round, replaced wholesale by
    #: each round and read by callers through this attribute.
    result: ThinkResult

    @abstractmethod
    async def start(
        self,
        req,
        system_prompt,
        tool_specs=None,
        *,
        model_route: "ModelRoute",
        model_call_id: str,
        duplicate_route: "ModelRoute | None" = None,
        resume: bool = False,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
    ) -> None:
        """Launch one think round.

        When ``tool_specs`` is provided the native tool-use channel is used;
        otherwise the XML text channel is used. ``llm`` is the per-request
        resolved client the loop passes after intelligent routing selects it.
        """

    @abstractmethod
    def reinstate(self, result: ThinkResult) -> None:
        """Prime *result* recovered from the durable journal, skipping the LLM.

        The durable resume path: instead of launching a think round, adopt a
        completed :class:`ThinkResult` the run journal memoized before the crash
        and mark the round finished, so ``done`` is True and the channel reads
        the reinstated result without ever re-calling the model (closes G1's
        re-pay window). Synchronous — no task is launched.
        """

    @abstractmethod
    async def join(self) -> None:
        """Await the in-flight think round and clean up its task."""

    @property
    @abstractmethod
    def done(self) -> bool:
        """True when no round is pending or the pending round has finished."""
