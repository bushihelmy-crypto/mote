#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the MediaProvider registry (generate_media/registry.py).

The registry is the pluggable-vendor seam for media generation, aligned with the
LLM provider registry: decorators declare provider identity while each
Application owns and resolves an isolated registry. All offline.
"""
from __future__ import annotations

import pytest

from mote.product.toolsets.builtin.generate_media.bootstrap import builtin_media_provider_registry
from mote.product.toolsets.builtin.generate_media.registry import MediaProvider, MediaProviderRegistry, media_provider
from mote.runtime.errors import ToolNotConfiguredError

pytestmark = pytest.mark.asyncio


class TestBuiltIn:
    async def test_four_kinds_registered_under_openai(self):
        registry = builtin_media_provider_registry()
        for kind in ("image", "audio", "music", "video"):
            assert (kind, "openai") in registry.providers

    async def test_registered_class_stamps_kind_and_provider(self):
        cls = builtin_media_provider_registry().providers[("image", "openai")]
        assert cls.kind == "image"
        assert cls.provider == "openai"
        assert issubclass(cls, MediaProvider)


def _cfg(kind: str, **fields):
    """Build a fake ``load_config()`` whose ``multimodal.{kind}_generation`` is ``fields``."""
    sub = type("G", (), fields)()
    mm = type("MM", (), {f"{kind}_generation": sub})()
    return type("Cfg", (), {"multimodal": mm})()


class TestFactory:
    """Factory resolution logic, exercised against a dedicated fake ``testkind``
    (real creators read many config fields in their ``__init__``; a fake kind
    isolates the resolve-and-construct behavior from creator internals)."""

    @pytest.fixture
    def _fakekind(self):
        made: dict = {}

        @media_provider("testkind", "openai")
        class _Default(MediaProvider):
            async def start_once(self, item, *, idempotency_key, timeout_seconds):
                return "default-id"

            async def poll_once(self, operation_id, state, *, timeout_seconds):
                return made

        @media_provider("testkind", "vendor2")
        class _Vendor2(MediaProvider):
            async def start_once(self, item, *, idempotency_key, timeout_seconds):
                return "vendor-id"

            async def poll_once(self, operation_id, state, *, timeout_seconds):
                return made

        registry = MediaProviderRegistry()
        registry.register("testkind", "openai", _Default)
        registry.register("testkind", "vendor2", _Vendor2)
        return registry, _Default, _Vendor2

    async def test_resolves_from_config_provider(self, _fakekind):
        registry, _default, vendor2 = _fakekind
        provider = registry.create(
            "testkind",
            _cfg("testkind", provider="vendor2").multimodal.testkind_generation,
        )
        assert isinstance(provider, vendor2)

    async def test_missing_provider_field_defaults_to_openai(self, _fakekind):
        registry, default, _vendor2 = _fakekind
        # A config lacking a ``provider`` attr → factory falls back to "openai".
        provider = registry.create("testkind", _cfg("testkind").multimodal.testkind_generation)
        assert isinstance(provider, default)

    async def test_unknown_provider_raises_naming_config_path(self, _fakekind):
        registry, _default, _vendor2 = _fakekind
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            registry.create(
                "testkind",
                _cfg("testkind", provider="nope").multimodal.testkind_generation,
            )
        msg = str(excinfo.value)
        assert "'nope'" in msg
        assert "multimodal.testkind_generation.provider" in msg
        assert "openai" in msg  # lists the available providers for the kind


class TestPluggability:
    async def test_register_and_resolve_a_vendor_provider(self):
        @media_provider("image", "_fakevendor")
        class _FakeVendor(MediaProvider):
            async def start_once(self, item, *, idempotency_key, timeout_seconds):
                return "fake-id"

            async def poll_once(self, operation_id, state, *, timeout_seconds):
                return {"summary": "fake", "results": []}

        registry = builtin_media_provider_registry()
        registry.register("image", "_fakevendor", _FakeVendor)
        assert _FakeVendor.kind == "image"
        assert _FakeVendor.provider == "_fakevendor"
        provider = registry.create("image", type("G", (), {"provider": "_fakevendor"})())
        assert isinstance(provider, _FakeVendor)
        assert await provider.start_once({}, idempotency_key="key", timeout_seconds=1) == "fake-id"
