"""BaseContextProvider — the narrow per-flow parameter-packing interface the loop depends on."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from mote.common.base import BudgetVerdict, LoopContext
from mote.roles.context_provider.request import ThinkRequest

if TYPE_CHECKING:
    from mote.common.interface import LLMClient
    from mote.common.schema import Message


class BaseContextProvider(ABC):
    """The per-flow parameter-packing interface the loop depends on.

    Counterpart to ``BaseThinkEngine`` / ``BaseToolExecutor``: it collapses the
    "what does each react flow need this turn" packing behind a small surface
    the loop calls. Making it an ABC lets the Role swap in an alternative
    assembler (different prompt strategy, retrieval-augmented context, ...)
    without the loop knowing which concrete provider is in play.

    Deliberately NARROW — like the ``MessageStore`` slice in
    ``mote.common.interface``: this ABC exposes only ``prepare()`` (the
    dynamic, per-turn think request) and ``loop_context()`` (the static observe
    + loop-control bundle). It does NOT expose the Role. A loop typed against
    ``BaseContextProvider`` therefore cannot reach into the Role through the
    provider, keeping role behavior in the Role and the loop role-agnostic.
    """

    @abstractmethod
    async def prepare(self) -> ThinkRequest:
        """Build the full ThinkEngine.start() input set for one react cycle."""

    @abstractmethod
    def loop_context(self) -> LoopContext:
        """Pack the static observe + loop-control parameters for one run()."""

    @abstractmethod
    async def resolve_llm(self, messages: Optional[list["Message"]] = None) -> "LLMClient":
        """Resolve the LLM the loop should use for this request via the router.

        Triggered lazily by the loop when an LLM is actually needed. When
        intelligent routing is enabled the router picks a model from the request
        signals (``messages``); otherwise the configured fixed model is used.
        """

    @abstractmethod
    async def enforce_budget(self) -> BudgetVerdict:
        """Rule on this agent's spend against its configured budget cap.

        Called by the loop before each think. Reads the agent's own accrued
        spend against the schema's ``max_cost`` cap, surfaces a soft warning on
        the observation plane once at 80% and a hard-stop event once at 100%,
        and returns a :class:`BudgetVerdict`. An unbudgeted agent
        (``max_cost <= 0``) always returns ``PROCEED`` without emitting.
        """
