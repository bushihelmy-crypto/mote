#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for request-local retry delay calculation."""
from __future__ import annotations

from mote.runtime.models.clients.retry import MAX_RETRY_AFTER_SECONDS, retry_delay


class _ErrWithRetryAfter(Exception):
    def __init__(self, retry_after: object) -> None:
        super().__init__("boom")
        self.retry_after = retry_after


class TestRetryDelay:
    def test_retry_after_is_honoured_and_capped(self) -> None:
        assert retry_delay(_ErrWithRetryAfter(12), 1) == 12.0
        assert retry_delay(_ErrWithRetryAfter(10_000), 1) == MAX_RETRY_AFTER_SECONDS

    def test_exponential_delay_is_bounded(self) -> None:
        assert retry_delay(Exception("transient"), 1) == 1.0
        assert retry_delay(Exception("transient"), 20) == 60.0
