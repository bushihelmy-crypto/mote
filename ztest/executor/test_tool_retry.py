#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.tool_retry.retryable_tool``.

The decorator wraps a tool ``call`` in a tenacity retry whose predicate
defaults to ``is_retryable`` — so ``RetryableToolError`` retries while a plain
(non-retryable) ``ToolError`` aborts on the first raise. ``reraise=True`` means
the original exception propagates after exhaustion. ``max_wait`` is set to ~0 in
these tests to keep them fast.
"""

from __future__ import annotations

import pytest

from mote.contracts.tool.errors import RetryableToolError, ToolError
from mote.runtime.tools.tool_retry import retryable_tool

pytestmark = pytest.mark.asyncio


class TestRetryToSuccess:
    async def test_retries_until_success(self):
        calls = {"n": 0}

        @retryable_tool(max_attempts=5, max_wait=0.001)
        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RetryableToolError("transient")
            return "ok"

        assert await flaky() == "ok"
        assert calls["n"] == 3


class TestExhaustion:
    async def test_reraises_after_max_attempts(self):
        calls = {"n": 0}

        @retryable_tool(max_attempts=3, max_wait=0.001)
        async def always_transient():
            calls["n"] += 1
            raise RetryableToolError("still failing")

        with pytest.raises(RetryableToolError, match="still failing"):
            await always_transient()
        assert calls["n"] == 3


class TestNonRetryable:
    async def test_plain_tool_error_not_retried(self):
        calls = {"n": 0}

        @retryable_tool(max_attempts=5, max_wait=0.001)
        async def bad_args():
            calls["n"] += 1
            raise ToolError("bad path")

        with pytest.raises(ToolError, match="bad path"):
            await bad_args()
        # Plain ToolError is non-retryable -> exactly one attempt.
        assert calls["n"] == 1


class TestRetryOnOverride:
    async def test_retry_on_exception_tuple(self):
        calls = {"n": 0}

        @retryable_tool(max_attempts=4, max_wait=0.001, retry_on=(ValueError,))
        async def value_flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("nope")
            return "done"

        assert await value_flaky() == "done"
        assert calls["n"] == 2

    async def test_retry_on_predicate(self):
        calls = {"n": 0}

        def only_keyerror(exc):
            return isinstance(exc, KeyError)

        @retryable_tool(max_attempts=4, max_wait=0.001, retry_on=only_keyerror)
        async def key_flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise KeyError("missing")
            return "done"

        assert await key_flaky() == "done"
        assert calls["n"] == 2

    async def test_retry_on_does_not_catch_other_types(self):
        calls = {"n": 0}

        @retryable_tool(max_attempts=4, max_wait=0.001, retry_on=(ValueError,))
        async def type_error():
            calls["n"] += 1
            raise RuntimeError("unhandled")

        with pytest.raises(RuntimeError):
            await type_error()
        assert calls["n"] == 1


class TestSyncCall:
    async def test_sync_function_retries(self):
        # The decorator works on a plain (sync) callable too; invoked here from
        # within an async test (module-level asyncio marker) — the call itself
        # runs synchronously.
        calls = {"n": 0}

        @retryable_tool(max_attempts=3, max_wait=0.001)
        def sync_flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RetryableToolError("transient")
            return "ok"

        assert sync_flaky() == "ok"
        assert calls["n"] == 2
