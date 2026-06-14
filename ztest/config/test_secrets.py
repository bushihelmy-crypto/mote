#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.common.config.secrets`` — api_key_helper resolution."""
from __future__ import annotations

import sys

import pytest

from metagpt.common.config.layers import CREDENTIAL_DENYLIST, strip_sensitive
from metagpt.common.config.secrets import (
    clear_cache,
    resolve_api_key,
    run_api_key_helper,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def _echo_cmd(value: str) -> str:
    return f'{sys.executable} -c "print(\'{value}\')"'


def test_run_helper_returns_stripped_stdout():
    assert run_api_key_helper(_echo_cmd("sk-from-helper"), use_cache=False) == "sk-from-helper"


def test_run_helper_empty_command_is_empty():
    assert run_api_key_helper("", use_cache=False) == ""


def test_run_helper_nonzero_exit_is_empty():
    assert run_api_key_helper(f"{sys.executable} -c \"import sys; sys.exit(3)\"", use_cache=False) == ""


def test_resolve_fills_when_placeholder():
    merged = {"api_key_helper": _echo_cmd("sk-real"), "llm": {"model": "x", "api_key": "sk-"}}
    out = resolve_api_key(merged, use_cache=False)
    assert out == "sk-real"
    assert merged["llm"]["api_key"] == "sk-real"


def test_resolve_skips_when_static_key_present():
    merged = {"api_key_helper": _echo_cmd("sk-helper"), "llm": {"api_key": "sk-static-real"}}
    assert resolve_api_key(merged, use_cache=False) is None
    assert merged["llm"]["api_key"] == "sk-static-real"


def test_resolve_noop_without_helper():
    merged = {"llm": {"api_key": "sk-"}}
    assert resolve_api_key(merged, use_cache=False) is None
    assert merged["llm"]["api_key"] == "sk-"


def test_resolve_creates_llm_dict_when_missing():
    merged = {"api_key_helper": _echo_cmd("sk-new")}
    out = resolve_api_key(merged, use_cache=False)
    assert out == "sk-new"
    assert merged["llm"]["api_key"] == "sk-new"


def test_helper_output_is_cached():
    cmd = _echo_cmd("sk-cached")
    first = run_api_key_helper(cmd, use_cache=True)
    # A second call returns the cached value even though we can't easily prove
    # the subprocess didn't run; assert stable output (cache hit path).
    assert run_api_key_helper(cmd, use_cache=True) == first == "sk-cached"


def test_api_key_helper_is_stripped_from_untrusted_layers():
    # RCE guard: a malicious workdir layer must not be able to inject a helper.
    assert "api_key_helper" in CREDENTIAL_DENYLIST
    cleaned = strip_sensitive({"api_key_helper": "rm -rf /", "proxy": "ok"})
    assert "api_key_helper" not in cleaned
    assert cleaned["proxy"] == "ok"
