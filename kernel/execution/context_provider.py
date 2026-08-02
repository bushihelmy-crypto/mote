"""Narrow context preparation interface consumed by execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from mote.contracts.model.inference import ResolvedInferenceTarget
from mote.kernel.execution.context import BudgetVerdict, ExecutionContext
from mote.kernel.execution.request import InferenceRequest


class BaseContextProvider(ABC):
    """The per-flow parameter-packing interface the engine depends on.

    Counterpart to ``BaseInferenceEngine`` / ``BaseToolExecutor``: it collapses the
    "what does each react flow need this turn" packing behind a small surface
    the engine calls. Making it an ABC lets the Role swap in an alternative
    assembler (different prompt strategy, retrieval-augmented context, ...)
    without the engine knowing which concrete provider is in play.

    Deliberately NARROW — like the ``MessageStore`` slice in
    ``mote.contracts.ports``: this ABC exposes only ``prepare()`` (the
    dynamic, per-turn think request) and ``execution_context()`` (the static observe
    + loop-control bundle). It does NOT expose the Role. A loop typed against
    ``BaseContextProvider`` therefore cannot reach into the Role through the
    provider, keeping role behavior in the Role and the flow role-agnostic.
    """

    @abstractmethod
    async def prepare(self) -> InferenceRequest:
        """Build the full InferenceEngine.start() input set for one react cycle."""

    @abstractmethod
    def execution_context(self) -> ExecutionContext:
        """Pack the static observe + loop-control parameters for one run()."""

    @abstractmethod
    async def resolve_inference_target(
        self,
        request: Optional[InferenceRequest] = None,
        *,
        model_call_id: str = "",
    ) -> ResolvedInferenceTarget:
        """Resolve the canonical model route for this request via the router.

        Triggered lazily by the flow when an LLM is actually needed. When
        intelligent routing is enabled the router picks a model from the request
        signals (``messages``); otherwise the configured fixed model is used.
        """

    @abstractmethod
    async def release_inference_target(self, target: ResolvedInferenceTarget) -> None:
        """Release a resolved target that was not transferred to inference."""

    @abstractmethod
    def finalize_for_model(self, request: InferenceRequest, target: ResolvedInferenceTarget) -> InferenceRequest:
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
