#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.product.presentation.humanize`."""
from __future__ import annotations

import pytest

from mote.orchestration.background_tasks.results.formatting import format_elapsed
from mote.product.presentation.humanize import format_token_count
from mote.runtime.resources.formatting import format_file_size


class TestFormatFileSize:
    @pytest.mark.parametrize(
        "size,expected",
        [
            (0, "0 bytes"),
            (512, "512 bytes"),
            (1023, "1023 bytes"),
            (1024, "1KB"),
            (1536, "1.5KB"),
            (1024 * 1024, "1MB"),
            (int(1024 * 1024 * 2.5), "2.5MB"),
            (1024 * 1024 * 1024, "1GB"),
        ],
    )
    def test_human_readable(self, size, expected):
        assert format_file_size(size) == expected

    def test_trailing_zero_stripped(self):
        # 2048 bytes == 2.0KB -> ".0" stripped -> "2KB".
        assert format_file_size(2048) == "2KB"


class TestFormatElapsed:
    def test_seconds(self):
        assert format_elapsed(5.25) == "5.2s"

    def test_minutes(self):
        assert format_elapsed(90) == "1m30s"

    def test_exact_minute(self):
        assert format_elapsed(60) == "1m0s"


class TestFormatTokenCount:
    def test_below_thousand(self):
        assert format_token_count(840) == "840"

    def test_one_decimal_k(self):
        assert format_token_count(3400) == "3.4k"

    def test_rounded_k(self):
        assert format_token_count(12000) == "12k"
