#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.config.layers`` — the merge engine.

Covers deep_merge (dict recurse / scalar override / list union+dedupe / type
mismatch), credential stripping, and the layer stack's precedence ordering and
provenance map.
"""
from __future__ import annotations

from mote.runtime.config.layers import CREDENTIAL_DENYLIST, ConfigLayer, ConfigLayerStack, deep_merge, strip_sensitive
from mote.runtime.config.sources import ConfigSource


def test_deep_merge_recurses_dicts_and_overrides_scalars():
    base = {"llm": {"model": "a", "temperature": 0.0}, "proxy": "old"}
    overlay = {"llm": {"model": "b"}, "proxy": "new"}
    merged = deep_merge(base, overlay)
    assert merged == {"llm": {"model": "b", "temperature": 0.0}, "proxy": "new"}
    # inputs are not mutated
    assert base["llm"]["model"] == "a"


def test_deep_merge_lists_union_dedupe_preserving_order():
    base = {"allow": ["a", "b"]}
    overlay = {"allow": ["b", "c"]}
    assert deep_merge(base, overlay) == {"allow": ["a", "b", "c"]}


def test_deep_merge_dedupes_dict_items_by_value():
    base = {"servers": [{"name": "x"}]}
    overlay = {"servers": [{"name": "x"}, {"name": "y"}]}
    assert deep_merge(base, overlay) == {"servers": [{"name": "x"}, {"name": "y"}]}


def test_deep_merge_type_mismatch_overlay_wins():
    assert deep_merge({"k": {"nested": 1}}, {"k": "scalar"}) == {"k": "scalar"}
    assert deep_merge({"k": [1, 2]}, {"k": {"a": 1}}) == {"k": {"a": 1}}


def test_strip_sensitive_removes_credential_keys_recursively():
    data = {
        "llm": {"model": "m", "api_key": "secret", "base_url": "http://evil", "oauth": {"x": 1}},
        "model_providers": {"p": {}},
        "search": {"api_key": "k2", "engine": "google"},
    }
    cleaned = strip_sensitive(data)
    assert cleaned == {"llm": {"model": "m"}, "search": {"engine": "google"}}
    # denylist is the documented set
    assert CREDENTIAL_DENYLIST == {"api_key", "base_url", "oauth", "model_providers", "api_key_helper"}


def test_layer_stack_higher_precedence_wins_regardless_of_insert_order():
    stack = ConfigLayerStack()
    # add out of order: PROJECT (high) first, USER (low) second
    stack.add(ConfigLayer(ConfigSource.PROJECT, {"llm": {"model": "project"}}))
    stack.add(ConfigLayer(ConfigSource.USER, {"llm": {"model": "user", "temperature": 0.5}}))
    merged = stack.effective()
    assert merged == {"llm": {"model": "project", "temperature": 0.5}}


def test_layer_stack_provenance_tracks_source_per_leaf():
    stack = ConfigLayerStack()
    stack.add(ConfigLayer(ConfigSource.USER, {"llm": {"model": "user", "temperature": 0.5}}))
    stack.add(ConfigLayer(ConfigSource.PROJECT, {"llm": {"model": "project"}}))
    prov = stack.provenance()
    assert prov["llm.model"] == "PROJECT"  # last writer wins
    assert prov["llm.temperature"] == "USER"
