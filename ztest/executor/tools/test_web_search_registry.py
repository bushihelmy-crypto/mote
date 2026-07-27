#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the SearchBackend registry (executor/tools/web_search_registry.py).

The registry is the pluggable-vendor seam for web search. Each Application owns
an isolated catalog. The built-in ``"provider"`` backend wraps
the Role's provider-native ``web_search`` capability. All offline.
"""
from __future__ import annotations

import pytest

from mote.product.toolsets.builtin.web_search_registry import (
    ProviderSearchBackend,
    SearchBackend,
    builtin_search_backend_registry,
)
from mote.runtime.errors import ToolNotConfiguredError

pytestmark = pytest.mark.asyncio


class _Cfg:
    """Stand-in for the resolved config.tools.web_search sub-config."""

    def __init__(self, backend: str = "provider", api_key: str = "", base_url: str = ""):
        self.backend = backend
        self.api_key = api_key
        self.base_url = base_url


class TestBuiltIn:
    async def test_provider_is_registered_builtin(self):
        registry = builtin_search_backend_registry()
        assert registry.backends["provider"] is ProviderSearchBackend
        assert ProviderSearchBackend.name == "provider"

    async def test_factory_default_selects_provider(self):
        backend = builtin_search_backend_registry().create(_Cfg(), provider_search=None)
        assert isinstance(backend, ProviderSearchBackend)
        assert backend.name == "provider"

    async def test_provider_delegates_to_capability(self):
        seen = {}

        async def _cap(query, *, allowed_domains=None, blocked_domains=None):
            seen["query"] = query
            seen["allowed_domains"] = allowed_domains
            return ["hit"]

        backend = builtin_search_backend_registry().create(_Cfg(), provider_search=_cap)
        hits = await backend.search("cats", allowed_domains=["a.com"])
        assert hits == ["hit"]
        assert seen == {"query": "cats", "allowed_domains": ["a.com"]}

    async def test_provider_without_capability_raises_not_implemented(self):
        # No capability bound → NotImplementedError (the WebSearch tool turns this
        # into ToolNotConfiguredError steering to a search-capable model).
        backend = builtin_search_backend_registry().create(_Cfg(), provider_search=None)
        with pytest.raises(NotImplementedError):
            await backend.search("cats")


class TestUnknownBackend:
    async def test_unregistered_name_raises_naming_config_path(self):
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            builtin_search_backend_registry().create(_Cfg(backend="nope"))
        msg = str(excinfo.value)
        assert "'nope'" in msg
        assert "tools.web_search.backend" in msg
        # Lists the available (registered) backends to guide the fix.
        assert "provider" in msg

    async def test_empty_backend_falls_back_to_provider(self):
        # A blank backend string uses the built-in default rather than erroring.
        backend = builtin_search_backend_registry().create(_Cfg(backend=""))
        assert isinstance(backend, ProviderSearchBackend)


class TestPluggability:
    async def test_register_and_resolve_a_vendor_backend(self):
        class _FakeVendor(SearchBackend):
            name = "_fakevendor"

            async def search(self, query, *, allowed_domains=None, blocked_domains=None):
                return [f"vendor:{query}:{self._config.api_key}"]

        registry = builtin_search_backend_registry()
        registry.register(_FakeVendor.name, _FakeVendor)
        cfg = _Cfg(backend="_fakevendor", api_key="k")  # pragma: allowlist secret
        backend = registry.create(cfg, provider_search=None)
        assert isinstance(backend, _FakeVendor)
        assert await backend.search("dogs") == ["vendor:dogs:k"]
