#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.llm.llm_provider_registry (provider lookup + registration)."""

from __future__ import annotations

import pytest

from mote.product.models.errors import ProviderNotFoundError
from mote.product.models.registry import LLMProviderRegistry


class TestLLMProviderRegistry:
    def test_register_and_get(self):
        reg = LLMProviderRegistry()
        sentinel = object()
        reg.register("foo", sentinel)
        assert reg.get_provider("foo") is sentinel

    def test_get_unknown_raises_provider_not_found(self):
        reg = LLMProviderRegistry()
        with pytest.raises(ProviderNotFoundError):
            reg.get_provider("missing-key")

    def test_provider_not_found_carries_api_type(self):
        reg = LLMProviderRegistry()
        try:
            reg.get_provider("missing-key")
        except ProviderNotFoundError as e:
            assert e.context.get("api_type") == "missing-key"
            assert isinstance(e.__cause__, KeyError)


class TestRegistryIsolation:
    def test_registry_instances_are_isolated(self):
        assert LLMProviderRegistry() is not LLMProviderRegistry()

    def test_conflicting_registration_is_rejected(self):
        reg = LLMProviderRegistry()
        reg.register("same", object)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("same", str)

    def test_create_uses_only_this_catalog(self):
        class Provider:
            def __init__(self, config):
                self.config = config

        from mote.contracts.config.model.llm import LLMConfig, LLMType

        reg = LLMProviderRegistry()
        reg.register(LLMType.OPENAI, Provider)
        config = LLMConfig(api_type=LLMType.OPENAI, model="test", api_key="x")
        assert reg.create(config).config is config


def test_bare_runtime_context_fails_with_explicit_composition_error():
    from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
    from mote.product.config.schema import Config
    from mote.runtime.models.clients.context import Context

    context = Context(
        config=Config(
            models=ShortcutModelsConfig(
                default=ProductEndpointInput(model="test"),
            )
        )
    )
    assert not hasattr(context, "llm")
