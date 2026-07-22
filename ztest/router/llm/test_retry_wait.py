#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``wait_retry_after``: honour a stamped ``retry_after`` else fallback."""
from __future__ import annotations

from tenacity import RetryCallState
from tenacity.wait import wait_base

from mote.router.llm._retry import MAX_RETRY_AFTER_SECONDS, wait_retry_after


class _FixedFallback(wait_base):
    """A deterministic fallback so we can prove delegation happened."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, retry_state: RetryCallState) -> float:
        self.calls += 1
        return self.value


def _state_with_exception(exc: BaseException | None) -> RetryCallState:
    """A ``RetryCallState`` whose last outcome failed with ``exc`` (or succeeded)."""
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})  # type: ignore[arg-type]
    if exc is not None:
        state.set_exception((type(exc), exc, exc.__traceback__))
    else:
        state.set_result(None)
    return state


class _ErrWithRetryAfter(Exception):
    def __init__(self, retry_after: object) -> None:
        super().__init__("boom")
        self.retry_after = retry_after


class TestWaitRetryAfter:
    def test_positive_retry_after_is_honoured(self) -> None:
        fallback = _FixedFallback(999.0)
        wait = wait_retry_after(fallback=fallback)
        result = wait(_state_with_exception(_ErrWithRetryAfter(12.0)))
        assert result == 12.0
        assert fallback.calls == 0

    def test_retry_after_is_capped(self) -> None:
        wait = wait_retry_after(fallback=_FixedFallback(1.0))
        result = wait(_state_with_exception(_ErrWithRetryAfter(10_000.0)))
        assert result == MAX_RETRY_AFTER_SECONDS

    def test_custom_cap(self) -> None:
        wait = wait_retry_after(fallback=_FixedFallback(1.0), max_wait=30.0)
        result = wait(_state_with_exception(_ErrWithRetryAfter(500.0)))
        assert result == 30.0

    def test_no_retry_after_delegates_to_fallback(self) -> None:
        fallback = _FixedFallback(7.5)
        wait = wait_retry_after(fallback=fallback)
        result = wait(_state_with_exception(Exception("no header")))
        assert result == 7.5
        assert fallback.calls == 1

    def test_zero_or_negative_retry_after_delegates(self) -> None:
        fallback = _FixedFallback(3.0)
        wait = wait_retry_after(fallback=fallback)
        assert wait(_state_with_exception(_ErrWithRetryAfter(0))) == 3.0
        assert wait(_state_with_exception(_ErrWithRetryAfter(-5))) == 3.0
        assert fallback.calls == 2

    def test_non_numeric_retry_after_delegates(self) -> None:
        fallback = _FixedFallback(4.0)
        wait = wait_retry_after(fallback=fallback)
        assert wait(_state_with_exception(_ErrWithRetryAfter("soon"))) == 4.0
        assert fallback.calls == 1

    def test_successful_outcome_delegates(self) -> None:
        fallback = _FixedFallback(2.0)
        wait = wait_retry_after(fallback=fallback)
        assert wait(_state_with_exception(None)) == 2.0
        assert fallback.calls == 1

    def test_default_fallback_is_exponential(self) -> None:
        # No explicit fallback → wait_random_exponential; a no-retry-after state
        # must return a non-negative float without raising.
        wait = wait_retry_after()
        result = wait(_state_with_exception(Exception("x")))
        assert isinstance(result, float)
        assert result >= 0.0

    def test_integer_retry_after_is_honoured(self) -> None:
        wait = wait_retry_after(fallback=_FixedFallback(99.0))
        assert wait(_state_with_exception(_ErrWithRetryAfter(8))) == 8.0
