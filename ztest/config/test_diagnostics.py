#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.diagnostics`` — strict mode + dump."""
from __future__ import annotations

import pytest

from mote.common.config.diagnostics import unknown_key_paths
from mote.common.config.loader import load_config
from mote.common.config.meta_config import Config
from mote.common.config.report import format_report
from mote.common.exception import UnknownConfigKeysError


def test_unknown_key_paths_flags_top_level_and_nested():
    data = {
        "llm": {"model": "x", "bogus_field": 1},  # nested unknown
        "proxy": "p",  # known scalar
        "totally_unknown": True,  # top-level unknown
    }
    unknown = set(unknown_key_paths(data, Config))
    assert "totally_unknown" in unknown
    assert "llm.bogus_field" in unknown
    # known keys are not reported
    assert "proxy" not in unknown
    assert "llm.model" not in unknown


def test_unknown_key_paths_empty_for_clean_config():
    data = {"llm": {"model": "x"}, "proxy": "p", "enable_router": True}
    assert unknown_key_paths(data, Config) == []


def test_unknown_subtree_not_descended():
    data = {"ghost": {"a": 1, "b": {"c": 2}}}
    # only the top of the unknown subtree is reported, not its children
    assert unknown_key_paths(data, Config) == ["ghost"]


def test_strict_load_raises_on_unknown_key():
    with pytest.raises(UnknownConfigKeysError) as exc:
        load_config(programmatic={"llm": {"model": "x"}, "nope_not_a_field": 1}, strict=True)
    assert "nope_not_a_field" in str(exc.value)
    assert "nope_not_a_field" in exc.value.unknown_paths


def test_lenient_load_ignores_unknown_key():
    cfg = load_config(programmatic={"llm": {"model": "x"}, "nope_not_a_field": 1})
    assert cfg.llm.model == "x"
    assert not hasattr(cfg, "nope_not_a_field")


def test_format_report_includes_layers_and_provenance():
    report = format_report()
    assert "# Config layers" in report
    assert "# Effective values and their source" in report
    assert "llm.model" in report


def test_format_report_redacts_secrets():
    report = format_report()
    # api_key value must never appear in plaintext in the dump
    assert "api_key = ***" in report or "api_key" not in report
