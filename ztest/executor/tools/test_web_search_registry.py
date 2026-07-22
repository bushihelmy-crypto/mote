#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the SearchBackend registry (executor/tools/web_search_registry.py).

The registry is the pluggable-vendor seam for web search: a
``@register_search_backend(name)`` decorator populates a singleton, and
``create_search_backend(config)`` resolves the active backend from
``config.tools.web_search.backend``. The built-in ``"provider"`` backend wraps
the Role's provider-native ``web_search`` capability. All offline.
"""
from __future__ import annotations

import pytest

from mote.common.exception import ToolNotConfiguredError
from mote.executor.tools.web_search_registry import (
    SEARCH_REGISTRY,
    ProviderSearchBackend,
    SearchBackend,
    create_search_backend,
    register_search_backend,
)

pytestmark = pytest.mark.asyncio


class _Cfg:
    """Stand-in for the resolved config.tools.web_search sub-config."""

    def __init__(self, backend: str = "provider", api_key: str = "", base_url: str = ""):
        self.backend = backend
        self.api_key = api_key
        self.base_url = base_url


class TestBuiltIn:
    async def test_provider_is_registered_builtin(self):
        assert "provider" in SEARCH_REGISTRY.backends
        assert SEARCH_REGISTRY.backends["provider"] is ProviderSearchBackend
        assert ProviderSearchBackend.name == "provider"

    async def test_factory_default_selects_provider(self):
        backend = create_search_backend(_Cfg(), provider_search=None)
        assert isinstance(backend, ProviderSearchBackend)
        assert backend.name == "provider"

    async def test_provider_delegates_to_capability(self):
        seen = {}

        async def _cap(query, *, allowed_domains=None, blocked_domains=None):
            seen["query"] = query
            seen["allowed_domains"] = allowed_domains
            return ["hit"]

        backend = create_search_backend(_Cfg(), provider_search=_cap)
        hits = await backend.search("cats", allowed_domains=["a.com"])
        assert hits == ["hit"]
        assert seen == {"query": "cats", "allowed_domains": ["a.com"]}

    async def test_provider_without_capability_raises_not_implemented(self):
        # No capability bound → NotImplementedError (the WebSearch tool turns this
        # into ToolNotConfiguredError steering to a search-capable model).
        backend = create_search_backend(_Cfg(), provider_search=None)
        with pytest.raises(NotImplementedError):
            await backend.search("cats")


class TestUnknownBackend:
    async def test_unregistered_name_raises_naming_config_path(self):
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            create_search_backend(_Cfg(backend="nope"))
        msg = str(excinfo.value)
        assert "'nope'" in msg
        assert "tools.web_search.backend" in msg
        # Lists the available (registered) backends to guide the fix.
        assert "provider" in msg

    async def test_empty_backend_falls_back_to_provider(self):
        # A blank backend string uses the built-in default rather than erroring.
        backend = create_search_backend(_Cfg(backend=""))
        assert isinstance(backend, ProviderSearchBackend)


class TestPluggability:
    async def test_register_and_resolve_a_vendor_backend(self):
        # A future direct-API vendor plugs in via the decorator + config, with no
        # change to the factory or the WebSearch tool. Register into the live
        # singleton, then clean up so the global registry is not polluted.
        @register_search_backend("_fakevendor")
        class _FakeVendor(SearchBackend):
            async def search(self, query, *, allowed_domains=None, blocked_domains=None):
                return [f"vendor:{query}:{self._config.api_key}"]

        try:
            assert _FakeVendor.name == "_fakevendor"
            cfg = _Cfg(backend="_fakevendor", api_key="k")  # pragma: allowlist secret
            backend = create_search_backend(cfg, provider_search=None)
            assert isinstance(backend, _FakeVendor)
            # Uses config credentials, ignores the (absent) provider capability.
            assert await backend.search("dogs") == ["vendor:dogs:k"]
        finally:
            SEARCH_REGISTRY.backends.pop("_fakevendor", None)
