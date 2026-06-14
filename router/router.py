#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLMRouter — the unified entry for three routing methods.

1. explicit  (``route``)          : caller names a model / passes an LLMConfig.
2. task map  (``route_for_task``) : caller declares a task; a map picks a model.
3. intelligent (``aroute``)       : a pluggable Strategy picks from request signals.

Replaces the old ``LLM()`` factory; the module-level ``LLM()`` here keeps
byte-for-byte behavior compatibility with that factory.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from metagpt.common.config.llm_config import LLMConfig
from metagpt.common.exception import ModelNotFoundError
from metagpt.common.logs import logger
from metagpt.router.llm.base_llm import BaseLLM
from metagpt.router.schema import ModelCard, RoutingDecision, RoutingRequest
from metagpt.router.strategy import RoutingStrategy, RuleBasedStrategy

if TYPE_CHECKING:
    from metagpt.router.llm.context import Context

# field name of the default named model in Config
DEFAULT_MODEL_NAME = "llm"

# Built-in routing tasks (imported by callers, e.g. Role/ContextManager).
COMPRESSION_TASK = "compression"  # ContextManager autocompact summarization
SUMMARY_TASK = "summary"  # Role.end_session summary

# Built-in task -> the Config LLMConfig field name that serves it. Each task is
# wired into ``task_map`` (in _auto_register_from_config) when its card exists.
# Add a new task-routed model by declaring its LLMConfig field on Config and a
# single row here — no code branch needed.
DEFAULT_TASK_MODELS: dict[str, str] = {
    COMPRESSION_TASK: "compress_llm",
    SUMMARY_TASK: "summary_llm",
}


class LLMRouter:
    """Routes requests to a concrete :class:`BaseLLM` via three methods.

    Instance construction + cost-manager wiring is delegated to ``Context``
    (``llm`` / ``llm_with_cost_manager_from_llm_config``) so cost managers are
    selected exactly as before.
    """

    def __init__(
        self,
        context: Optional["Context"] = None,
        *,
        strategy: Optional[RoutingStrategy] = None,
        task_map: Optional[dict[str, str]] = None,
    ):
        # lazy import keeps router.py free of a hard Context dependency at import
        # time (mirrors the old factory constructing Context() on demand).
        from metagpt.router.llm.context import Context

        self.context: "Context" = context or Context()
        self.strategy: RoutingStrategy = strategy or RuleBasedStrategy()
        self.task_map: dict[str, str] = dict(task_map or {})
        self._cards: dict[str, ModelCard] = {}
        self._instances: dict[str, BaseLLM] = {}
        self._default: str = DEFAULT_MODEL_NAME
        self._auto_register_from_config()

    # ------------------------------------------------------------------ setup
    def _auto_register_from_config(self) -> None:
        """Register every ``LLMConfig``-typed field on ``context.config``.

        Field name becomes the model name; ``llm`` is also the default.
        """
        config = self.context.config
        for field_name in type(config).model_fields:
            value = getattr(config, field_name, None)
            if isinstance(value, LLMConfig):
                self._cards[field_name] = ModelCard(name=field_name, llm_config=value)
        self._default = DEFAULT_MODEL_NAME
        # Default task map: built-in tasks route to their named cards when present.
        for task, model_name in DEFAULT_TASK_MODELS.items():
            if model_name in self._cards:
                self.task_map.setdefault(task, model_name)

    def register(
        self,
        name: str,
        llm_config: LLMConfig,
        *,
        description: str = "",
        tags: Optional[set[str]] = None,
        tier: int = 1,
        context_window: Optional[int] = None,
    ) -> ModelCard:
        """Programmatically register / override a named model card."""
        card = ModelCard(
            name=name,
            llm_config=llm_config,
            description=description,
            tags=set(tags or set()),
            tier=tier,
            context_window=context_window,
        )
        self._cards[name] = card
        self._instances.pop(name, None)  # invalidate any cached instance
        return card

    def map_task(self, task: str, name: str) -> None:
        """Maintain the task-type -> named-model map (routing method #2)."""
        self.task_map[task] = name

    def set_strategy(self, strategy: RoutingStrategy) -> None:
        """Swap the intelligent-routing strategy at runtime (rule <-> llm judge)."""
        self.strategy = strategy

    # -------------------------------------------------------------- building
    def _build(self, card_or_name) -> BaseLLM:
        """Lazily build + cache the BaseLLM for a card (or named card)."""
        if isinstance(card_or_name, ModelCard):
            name = card_or_name.name
            card = card_or_name
        else:
            name = card_or_name
            card = self._cards.get(name)

        if name in self._instances:
            return self._instances[name]

        if card is None:
            raise ModelNotFoundError(
                f"Unknown model name: {name!r}. Registered: {sorted(self._cards)}",
                requested=name,
                registered=sorted(self._cards),
            )

        if name == self._default:
            instance = self.context.llm()
        else:
            instance = self.context.llm_with_cost_manager_from_llm_config(card.llm_config)
        # Wire FALLBACK recovery: on a deterministic refusal (e.g. content policy),
        # the provider's recovery loop fails over to the next registered model.
        instance._fallback_supplier = self.make_fallback_supplier(exclude=name)
        self._instances[name] = instance
        return instance

    def make_fallback_supplier(self, *, exclude: Optional[str] = None) -> Callable[[], Optional[BaseLLM]]:
        """Build a stateful supplier yielding each registered model once (FALLBACK recovery).

        Each call returns the next not-yet-tried provider (skipping ``exclude`` and any
        that fail to build), or None when the candidates are exhausted — at which point
        the recovery loop re-raises. With a single registered model this yields nothing,
        so FALLBACK degrades to a no-op.
        """
        tried: set[str] = set()
        if exclude is not None:
            tried.add(exclude)

        def supplier() -> Optional[BaseLLM]:
            for candidate in self._cards:
                if candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    return self._build(candidate)
                except Exception as e:
                    logger.warning(f"Fallback build failed for {candidate!r}: {e}")
            return None

        return supplier

    # ----------------------------------------------- method 1: explicit route
    def route(self, *, name: Optional[str] = None, llm_config: Optional[LLMConfig] = None) -> BaseLLM:
        """Explicit routing: give an LLMConfig, a name, or neither (default).

        Equivalent to the old ``LLM()`` factory when given ``llm_config`` / nothing.
        """
        if llm_config is not None:
            return self.context.llm_with_cost_manager_from_llm_config(llm_config)
        if name is not None:
            return self._build(name)
        return self._build(self._default)

    # --------------------------------------------- method 2: task-map route
    def route_for_task(self, task: str) -> BaseLLM:
        """Task-map routing: look up ``task`` in ``task_map``, else default."""
        name = self.task_map.get(task)
        if name and name in self._cards:
            return self._build(name)
        logger.debug(f"No model mapped for task {task!r}; falling back to default {self._default!r}.")
        return self._build(self._default)

    # ----------------------------------------- method 3: intelligent route
    def _candidate_cards(self, candidates: Optional[list[str]]) -> dict[str, ModelCard]:
        if candidates is None:
            return dict(self._cards)
        return {n: self._cards[n] for n in candidates if n in self._cards}

    async def aroute_decision(
        self,
        request: RoutingRequest,
        *,
        candidates: Optional[list[str]] = None,
    ) -> tuple[BaseLLM, RoutingDecision]:
        """Intelligent routing returning both the LLM and the decision."""
        cards = self._candidate_cards(candidates) or dict(self._cards)
        decision = await self.strategy.select(cards, request, default=self._default)
        llm = self._build(decision.name)
        return llm, decision

    async def aroute(
        self,
        request: RoutingRequest,
        *,
        candidates: Optional[list[str]] = None,
    ) -> BaseLLM:
        """Intelligent routing (the core): run the strategy, build the winner."""
        llm, _ = await self.aroute_decision(request, candidates=candidates)
        return llm


# --------------------------------------------------------------- module-level
_default_router: Optional[LLMRouter] = None


def get_router(context: Optional["Context"] = None) -> LLMRouter:
    """Lazily build (and cache) a module-level default router.

    Passing an explicit ``context`` always builds a fresh router for it.
    """
    global _default_router
    if context is not None:
        return LLMRouter(context)
    if _default_router is None:
        _default_router = LLMRouter()
    return _default_router


def LLM(llm_config: Optional[LLMConfig] = None, context: Optional["Context"] = None) -> BaseLLM:
    """Drop-in replacement for the old factory ``LLM``.

    Behavior is identical: default llm when no config, else build from config.
    """
    return get_router(context).route(llm_config=llm_config)
