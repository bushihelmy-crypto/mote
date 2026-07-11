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

from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from mote.common.config.config.llm_config import LLMConfig
from mote.common.exception import ModelNotFoundError
from mote.common.logs import log_class, logger
from mote.router.llm.base_llm import BaseLLM
from mote.router.llm.context import Context
from mote.router.schema import ModelCard, RoutingDecision, RoutingRequest
from mote.router.strategy import RoutingStrategy, RuleBasedStrategy

if TYPE_CHECKING:
    from mote.common.interface import ContextReducer

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


class LLMVariant(Enum):
    """A build variant of a model card — the second half of the instance cache key.

    The same model name can back two differently-configured ``BaseLLM`` instances:

    - ``THINK``: the main path; carries the injected ``context_reducer`` so its
      recovery loop can shrink+re-issue an overflowing wire payload (COMPRESS).
    - ``COMPRESSION``: the instance the ContextManager's summarize reducer runs
      on; built reducer-less (``context_reducer=None``) so summarize's inner
      ``aask()`` cannot re-enter ``_compress`` → summarize → forever. The cycle is
      broken at the injection layer, no runtime guard needed.

    Keying the instance cache on ``(name, variant)`` keeps these from aliasing
    without a stringly-typed ``"name::compression"`` suffix sharing the model-name
    namespace.
    """

    THINK = "think"
    COMPRESSION = "compression"


@log_class(level="DEBUG")
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

        self.context: "Context" = context or Context()
        self.strategy: RoutingStrategy = strategy or RuleBasedStrategy()
        self.task_map: dict[str, str] = dict(task_map or {})
        self._cards: dict[str, ModelCard] = {}
        self._instances: dict[tuple[str, LLMVariant], BaseLLM] = {}
        self._default: str = DEFAULT_MODEL_NAME
        # Optional boundary-safe reducer the upper layer (Role's ContextManager)
        # injects for COMPRESS recovery; stamped onto every built/routed LLM so
        # the recovery loop can shrink+re-issue an overflowing wire payload. None
        # (standalone/test use) => COMPRESS degrades to a re-raise.
        self.context_reducer: Optional["ContextReducer"] = None
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
        # Invalidate every cached variant for this name (THINK + COMPRESSION),
        # not just one — a re-register must not leave a stale reducer-less
        # compression instance pinned to the old config.
        self._instances = {k: v for k, v in self._instances.items() if k[0] != name}
        return card

    def map_task(self, task: str, name: str) -> None:
        """Maintain the task-type -> named-model map (routing method #2)."""
        self.task_map[task] = name

    def set_strategy(self, strategy: RoutingStrategy) -> None:
        """Swap the intelligent-routing strategy at runtime (rule <-> llm judge)."""
        self.strategy = strategy

    # -------------------------------------------------------------- building
    def _build(self, card_or_name, *, variant: LLMVariant = LLMVariant.THINK) -> BaseLLM:
        """Lazily build + cache the BaseLLM for a card (or named card).

        ``variant`` selects the build shape (see :class:`LLMVariant`).
        ``COMPRESSION`` builds the instance the ContextManager's summarize reducer
        runs on: cached under its own ``(name, variant)`` key and *not* stamped
        with ``context_reducer``. Summarize issues its own inner ``aask()``, and if
        that instance carried the COMPRESS reducer a nested overflow would recurse
        ``_compress`` → summarize → forever. Leaving its reducer slot ``None``
        breaks that cycle at the injection layer (no runtime guard needed), while
        FALLBACK/ROTATE recovery are still wired — only the COMPRESS strategy is
        withheld. The per-variant cache key also keeps this reducer-less instance
        from aliasing the reducer-bearing one built for the same model on the main
        think path.
        """
        if isinstance(card_or_name, ModelCard):
            name = card_or_name.name
            card = card_or_name
        else:
            name = card_or_name
            card = self._cards.get(name)

        cache_key = (name, variant)
        if cache_key in self._instances:
            return self._instances[cache_key]

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
        # Wire COMPRESS recovery: shrink+re-issue an overflowing wire payload.
        # Withheld for the COMPRESSION variant (see docstring) so summarize's
        # inner aask cannot re-enter _compress.
        instance.context_reducer = None if variant is LLMVariant.COMPRESSION else self.context_reducer
        self._instances[cache_key] = instance
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
                    continue
            return None

        return supplier

    # ----------------------------------------------- method 1: explicit route
    def route(self, *, name: Optional[str] = None, llm_config: Optional[LLMConfig] = None) -> BaseLLM:
        """Explicit routing: give an LLMConfig, a name, or neither (default).

        Equivalent to the old ``LLM()`` factory when given ``llm_config`` / nothing.
        """
        if llm_config is not None:
            # This branch bypasses ``_build`` (fresh, uncached instance), so the
            # COMPRESS reducer must be stamped here too — the main think path
            # routes per-request through here with ``role.config.llm``.
            instance = self.context.llm_with_cost_manager_from_llm_config(llm_config)
            instance.context_reducer = self.context_reducer
            return instance
        if name is not None:
            return self._build(name)
        return self._build(self._default)

    # --------------------------------------------- method 2: task-map route
    def route_for_task(self, task: str) -> BaseLLM:
        """Task-map routing: look up ``task`` in ``task_map``, else default.

        The COMPRESSION task's instance is built reducer-less (the ``COMPRESSION``
        variant) so the ContextManager's summarize reducer — which runs on it —
        cannot re-enter the COMPRESS recovery loop. Other tasks (incl. SUMMARY, a
        top-level turn-end call that is not nested inside compression) keep the
        reducer so their own overflows still shrink+re-issue.
        """
        variant = LLMVariant.COMPRESSION if task == COMPRESSION_TASK else LLMVariant.THINK
        name = self.task_map.get(task)
        if name and name in self._cards:
            return self._build(name, variant=variant)
        return self._build(self._default, variant=variant)

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
def LLM(llm_config: Optional[LLMConfig] = None, context: Optional["Context"] = None) -> BaseLLM:
    """Drop-in replacement for the old factory ``LLM``.

    Behavior is identical: default llm when no config, else build from config.
    A router is a cheap, context-scoped object, so build one per call (its own
    instance cache lives for the returned LLM's resolution); no module state.
    """
    return LLMRouter(context).route(llm_config=llm_config)
