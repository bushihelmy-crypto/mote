#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.router.llm.recovery (RecoveryRunner failover loop)."""
from __future__ import annotations

import pytest

from metagpt.common.exception import (
    ContextWindowExceededError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContentPolicyError,
    LLMInvalidRequestStateError,
    MetaGPTError,
    RecoveryAction,
)
from metagpt.router.llm.recovery import RecoveryRunner


def _make_call(results):
    """Build a coroutine factory that yields/raises ``results`` in order."""
    seq = iter(results)

    async def call():
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    return call


class TestNoErrorPath:
    @pytest.mark.asyncio
    async def test_returns_immediately(self):
        runner = RecoveryRunner()
        result = await runner.run(_make_call(["ok"]))
        assert result == "ok"


class TestRetryAndAbort:
    @pytest.mark.asyncio
    async def test_retry_action_reraises(self):
        # LLMConnectionError.recovery == RETRY → owned by tenacity below → re-raise.
        runner = RecoveryRunner()
        with pytest.raises(LLMConnectionError):
            await runner.run(_make_call([LLMConnectionError("net")]))

    @pytest.mark.asyncio
    async def test_abort_action_reraises(self):
        # plain MetaGPTError.recovery == ABORT → re-raise.
        runner = RecoveryRunner()
        with pytest.raises(MetaGPTError):
            await runner.run(_make_call([MetaGPTError("nope")]))

    @pytest.mark.asyncio
    async def test_non_metagpt_error_propagates(self):
        runner = RecoveryRunner()
        with pytest.raises(ValueError):
            await runner.run(_make_call([ValueError("raw")]))


class TestMissingCallbacks:
    @pytest.mark.asyncio
    async def test_compress_without_compressor_reraises(self):
        runner = RecoveryRunner()  # no compressor
        with pytest.raises(ContextWindowExceededError):
            await runner.run(_make_call([ContextWindowExceededError("too big")]))

    @pytest.mark.asyncio
    async def test_rotate_without_rotator_reraises(self):
        runner = RecoveryRunner()
        with pytest.raises(LLMAuthenticationError):
            await runner.run(_make_call([LLMAuthenticationError("401")]))

    @pytest.mark.asyncio
    async def test_fallback_without_supplier_reraises(self):
        runner = RecoveryRunner()
        with pytest.raises(LLMContentPolicyError):
            await runner.run(_make_call([LLMContentPolicyError("policy")]))


class TestCompressRecovery:
    @pytest.mark.asyncio
    async def test_compress_then_succeed(self):
        calls = {"n": 0}
        compressed = {"n": 0}

        async def compressor(messages):
            compressed["n"] += 1
            return [{"role": "user", "content": "smaller"}]

        async def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ContextWindowExceededError("too big")
            return "recovered"

        runner = RecoveryRunner(compressor=compressor)
        result = await runner.run(call, messages=[{"role": "user", "content": "big"}])
        assert result == "recovered"
        assert compressed["n"] == 1


class TestRotateRecovery:
    @pytest.mark.asyncio
    async def test_rotate_then_succeed(self):
        calls = {"n": 0}

        def rotator():
            return True

        async def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMAuthenticationError("401")
            return "rotated-ok"

        runner = RecoveryRunner(credential_rotator=rotator)
        assert await runner.run(call) == "rotated-ok"

    @pytest.mark.asyncio
    async def test_rotator_returns_false_reraises(self):
        runner = RecoveryRunner(credential_rotator=lambda: False)
        with pytest.raises(LLMAuthenticationError):
            await runner.run(_make_call([LLMAuthenticationError("401")]))


class TestFallbackRecovery:
    @pytest.mark.asyncio
    async def test_fallback_sets_fallback_llm(self):
        sentinel = object()
        calls = {"n": 0}

        def supplier():
            return sentinel

        async def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMContentPolicyError("policy")
            return "fellback"

        runner = RecoveryRunner(fallback_supplier=supplier)
        assert await runner.run(call) == "fellback"
        assert runner.fallback_llm is sentinel

    @pytest.mark.asyncio
    async def test_exhausted_supplier_reraises(self):
        runner = RecoveryRunner(fallback_supplier=lambda: None)
        with pytest.raises(LLMContentPolicyError):
            await runner.run(_make_call([LLMContentPolicyError("policy")]))


class TestTransformerRecovery:
    @pytest.mark.asyncio
    async def test_transformer_repairs_then_succeed(self):
        calls = {"n": 0}

        async def transformer(messages, exc):
            return [{"role": "user", "content": "repaired"}]

        async def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMInvalidRequestStateError("bad state")
            return "repaired-ok"

        runner = RecoveryRunner(
            message_transformers={RecoveryAction.STRIP_REQUEST_STATE: transformer}
        )
        assert await runner.run(call, messages=[{"role": "user", "content": "x"}]) == "repaired-ok"

    @pytest.mark.asyncio
    async def test_transformer_none_reraises(self):
        async def transformer(messages, exc):
            return None

        runner = RecoveryRunner(
            message_transformers={RecoveryAction.STRIP_REQUEST_STATE: transformer}
        )
        with pytest.raises(LLMInvalidRequestStateError):
            await runner.run(_make_call([LLMInvalidRequestStateError("bad")]))

    @pytest.mark.asyncio
    async def test_missing_transformer_reraises(self):
        runner = RecoveryRunner(message_transformers={})
        with pytest.raises(LLMInvalidRequestStateError):
            await runner.run(_make_call([LLMInvalidRequestStateError("bad")]))


class TestBudget:
    @pytest.mark.asyncio
    async def test_exhausts_max_recoveries(self):
        # always raises a COMPRESS error; compressor always "succeeds" but the
        # call never recovers → after max_recoveries the runner re-raises.
        async def compressor(messages):
            return messages

        runner = RecoveryRunner(compressor=compressor, max_recoveries=2)
        with pytest.raises(ContextWindowExceededError):
            await runner.run(
                _make_call([ContextWindowExceededError("x")] * 10),
                messages=[{"role": "user", "content": "x"}],
            )
