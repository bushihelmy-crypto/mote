#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.llm.recovery (build_llm_strategies registry builder).

The recovery *loop* now lives in the leaf layer (``common.exception.RecoveryRunner``)
and is tested in ``ztest/exception/test_recovery.py``. This module covers the
LLM-specific **strategy registry** assembled by :func:`build_llm_strategies`: that
each injected capability maps to the right ``RecoveryAction`` with the right
``async (exc) -> bool`` behaviour, and that omitted capabilities are absent.
"""
from __future__ import annotations

import pytest

from mote.common.exception import MoteError, RecoveryAction
from mote.router.llm.recovery import build_llm_strategies

pytestmark = pytest.mark.asyncio


_EXC = MoteError("boom")


class TestRegistryShape:
    async def test_empty_when_no_capabilities(self):
        assert build_llm_strategies() == {}

    async def test_omits_none_capabilities(self):
        async def compress():
            return True

        registry = build_llm_strategies(compress=compress)
        assert set(registry) == {RecoveryAction.COMPRESS}
        assert RecoveryAction.ROTATE_CREDENTIAL not in registry
        assert RecoveryAction.FALLBACK not in registry

    async def test_all_capabilities_present(self):
        async def compress():
            return True

        async def transform(exc):
            return True

        registry = build_llm_strategies(
            compress=compress,
            rotate=lambda: True,
            fallback=lambda: object(),
            transformers={RecoveryAction.SHRINK_IMAGE: transform},
        )
        assert set(registry) == {
            RecoveryAction.COMPRESS,
            RecoveryAction.ROTATE_CREDENTIAL,
            RecoveryAction.FALLBACK,
            RecoveryAction.SHRINK_IMAGE,
        }


class TestCompressStrategy:
    async def test_delegates_to_compressor(self):
        calls = {"n": 0}

        async def compress():
            calls["n"] += 1
            return True

        registry = build_llm_strategies(compress=compress)
        assert await registry[RecoveryAction.COMPRESS](_EXC) is True
        assert calls["n"] == 1

    async def test_propagates_false(self):
        async def compress():
            return False

        registry = build_llm_strategies(compress=compress)
        assert await registry[RecoveryAction.COMPRESS](_EXC) is False


class TestRotateStrategy:
    async def test_returns_true_on_rotate(self):
        registry = build_llm_strategies(rotate=lambda: True)
        assert await registry[RecoveryAction.ROTATE_CREDENTIAL](_EXC) is True

    async def test_returns_false_when_no_credential(self):
        registry = build_llm_strategies(rotate=lambda: False)
        assert await registry[RecoveryAction.ROTATE_CREDENTIAL](_EXC) is False


class TestFallbackStrategy:
    async def test_notifies_sink_on_success(self):
        provider = object()
        seen = {}

        def on_fallback(p):
            seen["provider"] = p

        registry = build_llm_strategies(fallback=lambda: provider, on_fallback=on_fallback)
        assert await registry[RecoveryAction.FALLBACK](_EXC) is True
        assert seen["provider"] is provider

    async def test_returns_false_when_no_provider(self):
        called = {"n": 0}

        def on_fallback(p):
            called["n"] += 1

        registry = build_llm_strategies(fallback=lambda: None, on_fallback=on_fallback)
        assert await registry[RecoveryAction.FALLBACK](_EXC) is False
        assert called["n"] == 0  # sink not invoked when no provider

    async def test_works_without_sink(self):
        registry = build_llm_strategies(fallback=lambda: object())
        assert await registry[RecoveryAction.FALLBACK](_EXC) is True


class TestTransformers:
    async def test_passed_through_verbatim(self):
        seen = {}

        async def transform(exc):
            seen["exc"] = exc
            return True

        registry = build_llm_strategies(transformers={RecoveryAction.STRIP_REQUEST_STATE: transform})
        strategy = registry[RecoveryAction.STRIP_REQUEST_STATE]
        assert strategy is transform  # verbatim, no wrapping
        assert await strategy(_EXC) is True
        assert seen["exc"] is _EXC

    async def test_multiple_transformers(self):
        async def a(exc):
            return True

        async def b(exc):
            return False

        registry = build_llm_strategies(
            transformers={
                RecoveryAction.SHRINK_IMAGE: a,
                RecoveryAction.DOWNGRADE_TOOL_CONTENT: b,
            }
        )
        assert await registry[RecoveryAction.SHRINK_IMAGE](_EXC) is True
        assert await registry[RecoveryAction.DOWNGRADE_TOOL_CONTENT](_EXC) is False
