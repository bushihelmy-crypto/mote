#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.compress.registry.lookup_compressor``."""
from __future__ import annotations

import pytest

from metagpt.executor.compress.cargo import CargoCompressor
from metagpt.executor.compress.git import GitCompressor
from metagpt.executor.compress.pip import PipCompressor
from metagpt.executor.compress.pytest import PytestCompressor
from metagpt.executor.compress.registry import lookup_compressor
from metagpt.executor.compress.ruff import RuffCompressor


class TestFullPrefixRouting:
    @pytest.mark.parametrize(
        "prefix,argv,cls",
        [
            ("git status", ["git", "status"], GitCompressor),
            ("git log", ["git", "log"], GitCompressor),
            ("git diff", ["git", "diff"], GitCompressor),
            ("ruff check", ["ruff", "check"], RuffCompressor),
        ],
    )
    def test_routes_by_full_prefix(self, prefix, argv, cls):
        assert isinstance(lookup_compressor(prefix, argv), cls)


class TestHeadTokenRouting:
    @pytest.mark.parametrize(
        "prefix,argv,cls",
        [
            ("git", ["git"], GitCompressor),
            ("pytest", ["pytest"], PytestCompressor),
            ("flake8", ["flake8"], RuffCompressor),
            ("cargo", ["cargo", "test"], CargoCompressor),
            ("pip", ["pip", "install"], PipCompressor),
        ],
    )
    def test_routes_by_head_token(self, prefix, argv, cls):
        assert isinstance(lookup_compressor(prefix, argv), cls)


class TestDashMFallback:
    def test_python_dash_m_pytest(self):
        c = lookup_compressor("python", ["python", "-m", "pytest", "tests/"])
        assert isinstance(c, PytestCompressor)

    def test_python_dash_m_ruff(self):
        c = lookup_compressor("python", ["python", "-m", "ruff", "check"])
        assert isinstance(c, RuffCompressor)


class TestUnknown:
    @pytest.mark.parametrize("prefix,argv", [("ls", ["ls"]), ("echo", ["echo", "hi"]), ("", [])])
    def test_unknown_returns_none(self, prefix, argv):
        assert lookup_compressor(prefix, argv) is None

    def test_none_prefix_returns_none(self):
        assert lookup_compressor(None, None) is None
