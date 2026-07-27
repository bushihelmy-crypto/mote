"""BaseContextProvider — the narrow parameter-packing interface the flow depends on."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from mote.kernel.flow.context import BudgetVerdict, FlowContext
from mote.runtime.agent.context_provider.request import ThinkRequest

if TYPE_CHECKING:
    from mote.contracts.ports import ModelRoute


class BaseContextProvider(ABC):
    """The per-flow parameter-packing interface the engine depends on.

    Counterpart to ``BaseThinkEngine`` / ``BaseToolExecutor``: it collapses the
    "what does each react flow need this turn" packing behind a small surface
    the engine calls. Making it an ABC lets the Role swap in an alternative
    assembler (different prompt strategy, retrieval-augmented context, ...)
    without the engine knowing which concrete provider is in play.

    Deliberately NARROW — like the ``MessageStore`` slice in
    ``mote.contracts.ports``: this ABC exposes only ``prepare()`` (the
    dynamic, per-turn think request) and ``flow_context()`` (the static observe
    + loop-control bundle). It does NOT expose the Role. A loop typed against
    ``BaseContextProvider`` therefore cannot reach into the Role through the
    provider, keeping role behavior in the Role and the flow role-agnostic.
    """

    @abstractmethod
    async def prepare(self) -> ThinkRequest:
        """Build the full ThinkEngine.start() input set for one react cycle."""

    @abstractmethod
    def flow_context(self) -> FlowContext:
        """Pack the static observe + loop-control parameters for one run()."""

    @abstractmethod
    async def resolve_model_route(
        self,
        request: Optional[ThinkRequest] = None,
        *,
        model_call_id: str = "",
    ) -> "ModelRoute":
        """Resolve the canonical model route for this request via the router.

        Triggered lazily by the flow when an LLM is actually needed. When
        intelligent routing is enabled the router picks a model from the request
        signals (``messages``); otherwise the configured fixed model is used.
        """

    @abstractmethod
    def resolve_task_model_route(self, task: str) -> "ModelRoute":
        """Resolve one isolated secondary model route by task name."""

    @abstractmethod
    def finalize_for_model(self, request: ThinkRequest, route: "ModelRoute") -> ThinkRequest:
        """Bind semantic capabilities and canonical tools to the route."""

    @abstractmethod
    async def enforce_budget(self) -> BudgetVerdict:
        """Rule on this agent's spend against its configured budget cap.

        Called by the flow before each think. Reads the agent's own accrued
        spend against the schema's ``max_cost`` cap, surfaces a soft warning on
        the observation plane once at 80% and a hard-stop event once at 100%,
        and returns a :class:`BudgetVerdict`. An unbudgeted agent
        (``max_cost <= 0``) always returns ``PROCEED`` without emitting.
        """
