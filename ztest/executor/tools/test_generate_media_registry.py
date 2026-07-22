#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the MediaProvider registry (generate_media/registry.py).

The registry is the pluggable-vendor seam for media generation, aligned with the
LLM provider registry: a ``@register_media_provider(kind, name)`` decorator
populates a singleton keyed by ``(kind, name)``, and
``create_media_provider(kind)`` resolves the active provider from
``multimodal.{kind}_generation.provider`` (default ``"openai"``). All offline.
"""
from __future__ import annotations

import pytest

from mote.common.exception import ToolNotConfiguredError
from mote.executor.tools.generate_media import creators  # noqa: F401  (fires @register_media_provider)
from mote.executor.tools.generate_media.registry import (
    MEDIA_REGISTRY,
    MediaProvider,
    create_media_provider,
    register_media_provider,
)

pytestmark = pytest.mark.asyncio


class TestBuiltIn:
    async def test_four_kinds_registered_under_openai(self):
        for kind in ("image", "audio", "music", "video"):
            assert (kind, "openai") in MEDIA_REGISTRY.providers

    async def test_registered_class_stamps_kind_and_provider(self):
        cls = MEDIA_REGISTRY.providers[("image", "openai")]
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

        @register_media_provider("testkind", "openai")
        class _Default(MediaProvider):
            async def generate(self, items):
                return made

        @register_media_provider("testkind", "vendor2")
        class _Vendor2(MediaProvider):
            async def generate(self, items):
                return made

        yield _Default, _Vendor2
        MEDIA_REGISTRY.providers.pop(("testkind", "openai"), None)
        MEDIA_REGISTRY.providers.pop(("testkind", "vendor2"), None)

    async def test_resolves_from_config_provider(self, monkeypatch, _fakekind):
        from mote.executor.tools.generate_media import registry as reg

        _default, vendor2 = _fakekind
        monkeypatch.setattr(reg, "load_config", lambda *a, **k: _cfg("testkind", provider="vendor2"))
        provider = create_media_provider("testkind", output_dir="/tmp/x")
        assert isinstance(provider, vendor2)
        assert provider._output_dir_arg == "/tmp/x"

    async def test_missing_provider_field_defaults_to_openai(self, monkeypatch, _fakekind):
        from mote.executor.tools.generate_media import registry as reg

        default, _vendor2 = _fakekind
        # A config lacking a ``provider`` attr → factory falls back to "openai".
        monkeypatch.setattr(reg, "load_config", lambda *a, **k: _cfg("testkind"))
        provider = create_media_provider("testkind")
        assert isinstance(provider, default)

    async def test_unknown_provider_raises_naming_config_path(self, monkeypatch, _fakekind):
        from mote.executor.tools.generate_media import registry as reg

        monkeypatch.setattr(reg, "load_config", lambda *a, **k: _cfg("testkind", provider="nope"))
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            create_media_provider("testkind")
        msg = str(excinfo.value)
        assert "'nope'" in msg
        assert "multimodal.testkind_generation.provider" in msg
        assert "openai" in msg  # lists the available providers for the kind


class TestPluggability:
    async def test_register_and_resolve_a_vendor_provider(self, monkeypatch):
        from mote.executor.tools.generate_media import registry as reg

        @register_media_provider("image", "_fakevendor")
        class _FakeVendor(MediaProvider):
            async def generate(self, items):
                return {"summary": "fake", "results": []}

        try:
            assert _FakeVendor.kind == "image"
            assert _FakeVendor.provider == "_fakevendor"
            cfg = type(
                "Cfg",
                (),
                {"multimodal": type("MM", (), {"image_generation": type("G", (), {"provider": "_fakevendor"})()})()},
            )()
            monkeypatch.setattr(reg, "load_config", lambda *a, **k: cfg)
            provider = create_media_provider("image")
            assert isinstance(provider, _FakeVendor)
            assert await provider.generate([]) == {"summary": "fake", "results": []}
        finally:
            MEDIA_REGISTRY.providers.pop(("image", "_fakevendor"), None)
