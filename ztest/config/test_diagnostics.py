#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for strict config diagnostics and redacted reporting."""
from __future__ import annotations

import pytest

from mote.product.config.diagnostics import unknown_key_paths
from mote.product.config.loader import load_config
from mote.product.config.report import format_report
from mote.product.config.schema import Config
from mote.runtime.errors import UnknownConfigKeysError


def test_unknown_key_paths_flags_top_level_and_nested():
    data = {
        "models": {"mode": "shortcut", "default": {"model": "x", "bogus_field": 1}},
        "tools": {"proxy": "p"},  # known scalar
        "totally_unknown": True,  # top-level unknown
    }
    unknown = set(unknown_key_paths(data, Config))
    assert "totally_unknown" in unknown
    assert "models.default.bogus_field" in unknown
    # known keys are not reported
    assert "tools.proxy" not in unknown
    assert "models.default.model" not in unknown


def test_unknown_key_paths_empty_for_clean_config():
    data = {
        "models": {
            "mode": "shortcut",
            "default": {"model": "x"},
            "api_key_helper": "cmd",
        },  # pragma: allowlist secret
        "tools": {"proxy": "p"},
    }
    assert unknown_key_paths(data, Config) == []


def test_unknown_subtree_not_descended():
    data = {"ghost": {"a": 1, "b": {"c": 2}}}
    # only the top of the unknown subtree is reported, not its children
    assert unknown_key_paths(data, Config) == ["ghost"]


def test_load_raises_on_unknown_key():
    with pytest.raises(UnknownConfigKeysError) as exc:
        load_config(
            programmatic={"models": {"mode": "shortcut", "default": {"model": "x"}}, "nope_not_a_field": 1},
        )
    assert "nope_not_a_field" in str(exc.value)
    assert "nope_not_a_field" in exc.value.unknown_paths


def test_format_report_includes_layers_and_provenance(_explicit_product_config_root):
    report = format_report(source_root=_explicit_product_config_root)
    assert "# Config layers" in report
    assert "# Effective values and their source" in report
    assert "models.default.model" in report


def test_format_report_redacts_secrets(_explicit_product_config_root):
    report = format_report(source_root=_explicit_product_config_root)
    # api_key value must never appear in plaintext in the dump
    assert "api_key = ***" in report or "api_key" not in report
