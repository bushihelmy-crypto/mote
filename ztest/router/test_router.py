#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.router (LLMRouter — three routing methods + fallback)."""
from __future__ import annotations

import pytest

from mote.common.config.config.llm_config import LLMConfig
from mote.common.exception import ModelNotFoundError
from mote.router.schema import RoutingRequest
from mote.router.strategy import RuleBasedStrategy

from .conftest import FakeLLM

# The ``router`` fixture (conftest) wires a deterministic 4-card ladder + an
# ``llm`` default, stubbing context.llm / llm_with_cost_manager_from_llm_config.


class TestExplicitRoute:
    def test_route_default(self, router):
        llm = router.route()
        assert isinstance(llm, FakeLLM)
        assert llm.name == "llm"

    def test_route_by_name(self, router):
        llm = router.route(name="strong")
        # non-default card built via llm_with_cost_manager_from_llm_config (model name)
        assert llm.name == "claude-3-haiku"

    def test_route_by_llm_config(self, router):
        cfg = LLMConfig(api_key="sk-test", model="custom-model")
        llm = router.route(llm_config=cfg)
        assert llm.name == "custom-model"

    def test_route_unknown_name_raises(self, router):
        with pytest.raises(ModelNotFoundError):
            router.route(name="does-not-exist")

    def test_build_is_cached(self, router):
        a = router.route(name="strong")
        b = router.route(name="strong")
        assert a is b


class TestTaskRoute:
    def test_unmapped_task_falls_back_to_default(self, router):
        llm = router.route_for_task("no-such-task")
        assert llm.name == "llm"

    def test_mapped_task(self, router):
        router.map_task("special", "strong")
        llm = router.route_for_task("special")
        assert llm.name == "claude-3-haiku"


class TestRegister:
    def test_register_adds_card(self, router):
        router.register("extra", LLMConfig(api_key="sk-test", model="extra-model"), tier=2)
        assert "extra" in router._cards
        llm = router.route(name="extra")
        assert llm.name == "extra-model"

    def test_register_invalidates_cache(self, router):
        first = router.route(name="strong")
        router.register("strong", LLMConfig(api_key="sk-test", model="new-strong"))
        second = router.route(name="strong")
        assert second is not first
        assert second.name == "new-strong"

    def test_register_invalidates_all_variants(self, router):
        # A re-register must drop EVERY cached variant for the name — including the
        # reducer-less COMPRESSION instance — not just the THINK one, or a stale
        # compression instance stays pinned to the old config.
        from mote.router.router import COMPRESSION_TASK

        router.map_task(COMPRESSION_TASK, "strong")
        first = router.route_for_task(COMPRESSION_TASK)
        router.register("strong", LLMConfig(api_key="sk-test", model="new-strong"))
        second = router.route_for_task(COMPRESSION_TASK)
        assert second is not first
        assert second.name == "new-strong"


class TestIntelligentRoute:
    @pytest.mark.asyncio
    async def test_aroute_returns_llm(self, router):
        router.set_strategy(RuleBasedStrategy())
        req = RoutingRequest(text="describe this image", requires_vision=True)
        llm = await router.aroute(req)
        # vision card is gpt-4o → name "gpt-4o"
        assert llm.name == "gpt-4o"

    @pytest.mark.asyncio
    async def test_aroute_decision_returns_both(self, router):
        req = RoutingRequest(text="x", prefer_cheap=True)
        llm, decision = await router.aroute_decision(req)
        assert decision.name == "cheap"
        assert llm.name == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_aroute_with_candidate_subset(self, router):
        req = RoutingRequest(text="x", flags={"high_risk"})
        # restrict to cheap + mid only → strongest among them is mid.
        llm, decision = await router.aroute_decision(req, candidates=["cheap", "mid"])
        assert decision.name == "mid"


class TestFallbackSupplier:
    def test_supplier_yields_each_once_then_none(self, router):
        supplier = router.make_fallback_supplier(exclude="llm")
        seen = set()
        while True:
            llm = supplier()
            if llm is None:
                break
            seen.add(llm.name)
        # excluded "llm" default never yielded; others appear (by model name).
        assert "deepseek-chat" in seen
        assert len(seen) >= 1

    def test_built_instance_gets_fallback_supplier(self, router):
        llm = router.route(name="strong")
        assert llm._fallback_supplier is not None
        assert callable(llm._fallback_supplier)


class TestContextReducerInjection:
    """The COMPRESS-recovery reducer is stamped onto every built/routed LLM,
    EXCEPT the dedicated COMPRESSION instance (built reducer-less so the
    ContextManager's summarize reducer can't re-enter the COMPRESS loop)."""

    def test_built_instance_gets_reducer(self, router):
        sentinel = object()
        router.context_reducer = sentinel
        llm = router.route(name="strong")
        assert llm.context_reducer is sentinel

    def test_route_by_llm_config_gets_reducer(self, router):
        # The main think path bypasses _build via route(llm_config=), so it must
        # be stamped there too.
        from mote.common.config.config.llm_config import LLMConfig

        sentinel = object()
        router.context_reducer = sentinel
        llm = router.route(llm_config=LLMConfig(api_key="sk-test", model="gpt-4o"))
        assert llm.context_reducer is sentinel

    def test_default_none_when_unset(self, router):
        llm = router.route(name="strong")
        assert llm.context_reducer is None

    def test_compression_instance_has_no_reducer(self, router):
        # The summarize reducer runs on this instance and issues its own inner
        # aask(); withholding the reducer breaks the _compress → summarize cycle
        # at the injection layer (no runtime guard).
        from mote.router.router import COMPRESSION_TASK

        sentinel = object()
        router.context_reducer = sentinel
        llm = router.route_for_task(COMPRESSION_TASK)
        assert llm.context_reducer is None

    def test_compression_instance_falls_back_to_default_reducer_less(self, router):
        # With no compression model *card* registered, the task-map entry
        # (compress_llm) can't resolve, so the task routes to the default — but
        # still reducer-less, so an unconfigured compression model can't recurse.
        from mote.router.router import COMPRESSION_TASK

        sentinel = object()
        router.context_reducer = sentinel
        assert router.task_map.get(COMPRESSION_TASK) not in router._cards
        llm = router.route_for_task(COMPRESSION_TASK)
        assert llm.context_reducer is None

    def test_compression_instance_keeps_fallback_supplier(self, router):
        # Only COMPRESS is withheld; FALLBACK/ROTATE recovery stay wired.
        from mote.router.router import COMPRESSION_TASK

        llm = router.route_for_task(COMPRESSION_TASK)
        assert llm._fallback_supplier is not None
        assert callable(llm._fallback_supplier)

    def test_compression_instance_cached_separately_from_think_instance(self, router):
        # The reducer-less compression instance must not alias the reducer-bearing
        # instance built for the same model on the main think path.
        from mote.router.router import COMPRESSION_TASK

        sentinel = object()
        router.context_reducer = sentinel
        router.map_task(COMPRESSION_TASK, "strong")
        compression_llm = router.route_for_task(COMPRESSION_TASK)
        think_llm = router.route(name="strong")
        assert compression_llm is not think_llm
        assert compression_llm.context_reducer is None
        assert think_llm.context_reducer is sentinel

    def test_non_compression_task_keeps_reducer(self, router):
        # A non-compression task is a top-level call (not nested inside
        # compression), so it keeps its reducer — only COMPRESSION is withheld.
        sentinel = object()
        router.context_reducer = sentinel
        router.map_task("some-task", "strong")
        llm = router.route_for_task("some-task")
        assert llm.context_reducer is sentinel


class TestSharedEngine:
    """The process-level shared ML engine memoizes on the resolved model_dir."""

    def test_same_dir_returns_same_instance(self, tmp_path):
        from mote.router.ml.engine import shared_engine

        a = shared_engine(tmp_path / "bundle")
        b = shared_engine(tmp_path / "bundle")
        assert a is b

    def test_different_dir_returns_different_instance(self, tmp_path):
        from mote.router.ml.engine import shared_engine

        a = shared_engine(tmp_path / "one")
        b = shared_engine(tmp_path / "two")
        assert a is not b

    def test_default_dir_memoized(self):
        from mote.router.ml.engine import shared_engine

        assert shared_engine() is shared_engine()


class TestBuildRouterStrategy:
    """_build_router picks a strategy per agent kind (main vs sub); None = inert.

    The main/sub discriminator is ``role.state.parent_session_id`` — None for
    the root/main agent, a non-empty string for a spawned child.
    """

    def _ctx(self, *, main=None, sub=None, is_sub=False):
        import types

        from mote.common.config.config.router_config import AgentRouterConfig, RouterConfig
        from mote.router.llm.context import Context

        context = Context()
        router_cfg = RouterConfig(
            main_agent=AgentRouterConfig(strategy=main),
            sub_agent=AgentRouterConfig(strategy=sub),
        )
        state = types.SimpleNamespace(parent_session_id=("parent" if is_sub else None))
        role = types.SimpleNamespace(
            context=context,
            state=state,
            config=types.SimpleNamespace(router=router_cfg),
        )
        return types.SimpleNamespace(role=role)

    def test_default_none_is_inert(self):
        # Default (both None) → routing disabled, default RuleBasedStrategy left
        # in place but never consulted (routing_enabled False).
        from mote.roles.runtime_modules.cognition import _build_router
        from mote.router.strategy import RuleBasedStrategy

        built = _build_router(self._ctx())
        assert built.routing_enabled is False
        assert isinstance(built.strategy, RuleBasedStrategy)

    def test_main_rule_installs_rule_based(self):
        from mote.roles.runtime_modules.cognition import _build_router
        from mote.router.strategy import RuleBasedStrategy

        built = _build_router(self._ctx(main="rule"))
        assert built.routing_enabled is True
        assert isinstance(built.strategy, RuleBasedStrategy)

    def test_main_squilla_installs_squilla_strategy(self):
        from mote.roles.runtime_modules.cognition import _build_router
        from mote.router.squilla import SquillaStrategy

        built = _build_router(self._ctx(main="squilla"))
        assert built.routing_enabled is True
        assert isinstance(built.strategy, SquillaStrategy)

    def test_sub_agent_reads_sub_config(self):
        # A spawned child reads sub_agent, ignoring main_agent entirely.
        from mote.roles.runtime_modules.cognition import _build_router
        from mote.router.squilla import SquillaStrategy

        built = _build_router(self._ctx(main="rule", sub="squilla", is_sub=True))
        assert built.routing_enabled is True
        assert isinstance(built.strategy, SquillaStrategy)

    def test_main_agent_ignores_sub_config(self):
        # The root agent reads main_agent; a squilla sub_agent must not leak in.
        from mote.roles.runtime_modules.cognition import _build_router

        built = _build_router(self._ctx(main=None, sub="squilla", is_sub=False))
        assert built.routing_enabled is False

    def test_sub_none_is_inert_even_if_main_routes(self):
        from mote.roles.runtime_modules.cognition import _build_router

        built = _build_router(self._ctx(main="squilla", sub=None, is_sub=True))
        assert built.routing_enabled is False

    def test_squilla_strategy_shares_engine(self):
        from mote.roles.runtime_modules.cognition import _build_router
        from mote.router.ml.engine import shared_engine

        a = _build_router(self._ctx(main="squilla"))
        b = _build_router(self._ctx(main="squilla"))
        # Both per-role strategies share the one process engine (default dir).
        assert a.strategy.engine is b.strategy.engine is shared_engine()


class TestSeedSessionCapability:
    """The Agent tool's ``_seed`` guard is a getattr no-op for rule-based
    children: only SquillaStrategy exposes ``seed_session``."""

    def test_rule_based_has_no_seed_session(self):
        from mote.router.strategy import RuleBasedStrategy

        assert getattr(RuleBasedStrategy(), "seed_session", None) is None

    def test_squilla_exposes_seed_session(self, tmp_path):
        from mote.router.squilla import SquillaStrategy

        strat = SquillaStrategy(model_dir=tmp_path / "no-bundle")
        assert callable(getattr(strat, "seed_session", None))


class TestBreakerWiring:
    """Every built/routed LLM is stamped with the shared health registry + bus hook."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        # The registry is a process-global singleton; isolate it per test so
        # the installed transition hook / breakers don't leak across the suite.
        from mote.common.resilience import reset_health_registry

        reset_health_registry()
        yield
        reset_health_registry()

    def test_build_wires_shared_registry_and_bus_hook(self, router):
        from mote.common.events import breaker_bus_hook
        from mote.common.resilience import get_health_registry

        llm = router.route(name="strong")
        assert llm._health_registry is get_health_registry()
        # The bus-mirror hook is installed so breaker transitions emit events.
        assert get_health_registry()._on_transition is breaker_bus_hook

    def test_all_routes_share_one_registry(self, router):
        a = router.route(name="strong")
        b = router.route(name="mid")
        c = router.route()  # default
        d = router.route(llm_config=LLMConfig(api_key="sk-test", model="custom"))
        registries = {id(x._health_registry) for x in (a, b, c, d)}
        assert len(registries) == 1  # one shared registry across every path

    def test_llm_config_branch_wires_registry(self, router):
        from mote.common.resilience import get_health_registry

        # The llm_config branch bypasses ``_build`` (fresh instance) but must
        # still stamp the registry — it is the main think path.
        llm = router.route(llm_config=LLMConfig(api_key="sk-test", model="custom"))
        assert llm._health_registry is get_health_registry()
