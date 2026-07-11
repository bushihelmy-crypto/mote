#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.llm.llm_provider_registry (provider lookup + registration)."""
from __future__ import annotations

import pytest
from mote.common.exception import ProviderNotFoundError
from mote.router.llm.llm_provider_registry import LLMProviderRegistry, register_provider


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


class TestRegisterProviderDecorator:
    def test_singleton_shared_registry(self):
        # LLMProviderRegistry uses the Singleton metaclass: instances are shared.
        assert LLMProviderRegistry() is LLMProviderRegistry()

    def test_decorator_registers_single_key(self):
        @register_provider("decorated-single")
        class _P:
            pass

        assert LLMProviderRegistry().get_provider("decorated-single") is _P

    def test_decorator_registers_list_of_keys(self):
        @register_provider(["k1", "k2"])
        class _Q:
            pass

        reg = LLMProviderRegistry()
        assert reg.get_provider("k1") is _Q
        assert reg.get_provider("k2") is _Q
