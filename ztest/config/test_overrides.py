#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.overrides`` — -c parsing + ConfigOverrides."""
from __future__ import annotations

import pytest
from mote.common.config.overrides import ConfigOverrides, parse_cli_overrides, parse_override_value, set_nested


def test_parse_override_value_types():
    assert parse_override_value("true") is True
    assert parse_override_value("8000") == 8000
    assert parse_override_value("0.5") == 0.5
    assert parse_override_value("claude-opus-4-6") == "claude-opus-4-6"
    assert parse_override_value("") == ""


def test_set_nested_creates_and_replaces_scalar_intermediate():
    data = {"llm": "scalar"}
    set_nested(data, ["llm", "model"], "x")
    assert data == {"llm": {"model": "x"}}


def test_parse_cli_overrides_dotted_path_and_first_equals_split():
    out = parse_cli_overrides(["llm.model=claude", "enable_router=true", "llm.max_token=8000"])
    assert out == {"llm": {"model": "claude", "max_token": 8000}, "enable_router": True}


def test_parse_cli_overrides_value_may_contain_equals():
    out = parse_cli_overrides(["llm.base_url=https://x/v1?a=b"])
    assert out["llm"]["base_url"] == "https://x/v1?a=b"


def test_parse_cli_overrides_rejects_malformed():
    with pytest.raises(ValueError):
        parse_cli_overrides(["no-equals-sign"])
    with pytest.raises(ValueError):
        parse_cli_overrides(["=value"])


def test_parse_cli_overrides_empty_is_empty():
    assert parse_cli_overrides(None) == {}
    assert parse_cli_overrides([]) == {}


def test_config_overrides_to_layer_dict_maps_known_fields():
    ov = ConfigOverrides(model="m", api_key="k", base_url="u", proxy="p", enable_router=True)
    assert ov.to_layer_dict() == {
        "llm": {"model": "m", "api_key": "k", "base_url": "u"},
        "proxy": "p",
        "enable_router": True,
    }


def test_config_overrides_extra_deep_merges_and_wins():
    ov = ConfigOverrides(model="m", extra={"llm": {"temperature": 0.2}, "embedding": {"api_type": "x"}})
    out = ov.to_layer_dict()
    assert out["llm"] == {"model": "m", "temperature": 0.2}
    assert out["embedding"] == {"api_type": "x"}


def test_config_overrides_empty_is_empty_dict():
    assert ConfigOverrides().to_layer_dict() == {}
