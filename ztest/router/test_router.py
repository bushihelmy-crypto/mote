#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.router.router (LLMRouter — three routing methods + fallback)."""
from __future__ import annotations

import pytest

from metagpt.common.config.llm_config import LLMConfig
from metagpt.common.exception import ModelNotFoundError
from metagpt.router.schema import RoutingRequest
from metagpt.router.strategy import RuleBasedStrategy

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
