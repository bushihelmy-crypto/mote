#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.env`` — env var -> nested layer mapping."""
from __future__ import annotations

from mote.common.config.env import build_env_layer


def test_double_underscore_nests_single_underscore_stays_in_segment():
    env = {"MOTE_LLM__BASE_URL": "https://x/v1", "MOTE_LLM__MODEL": "claude"}
    assert build_env_layer(env) == {"llm": {"base_url": "https://x/v1", "model": "claude"}}


def test_values_are_yaml_typed():
    env = {"MOTE_ENABLE_ROUTER": "true", "MOTE_LLM__MAX_TOKEN": "8000"}
    out = build_env_layer(env)
    assert out["enable_router"] is True
    assert out["llm"]["max_token"] == 8000


def test_both_prefixes_supported():
    out = build_env_layer({"MOTE_PROXY": "http://p", "MOTE_LANGUAGE": "zh"})
    assert out["proxy"] == "http://p"
    assert out["language"] == "zh"


def test_non_prefixed_and_bare_prefix_ignored():
    env = {"PATH": "/usr/bin", "HOME": "/home/x", "MOTE_": "ignored"}
    assert build_env_layer(env) == {}


def test_empty_environ_yields_empty_layer():
    assert build_env_layer({}) == {}
