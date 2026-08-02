#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for oauth-aware LLMConfig key validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.config.errors import MissingAPIKeyError
from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.config.model.oauth import OAuthProviderConfig


def _oauth() -> OAuthProviderConfig:
    return OAuthProviderConfig(token_url="https://issuer/token", client_id="cid")


def test_static_key_still_required_without_oauth():
    with pytest.raises((MissingAPIKeyError, ValidationError)):
        LLMConfig(api_key="")


def test_placeholder_key_rejected_without_oauth():
    with pytest.raises((MissingAPIKeyError, ValidationError)):
        LLMConfig(api_key="YOUR_API_KEY")


def test_valid_static_key_ok():
    cfg = LLMConfig(api_key="sk-real")
    assert cfg.oauth is None


def test_oauth_allows_empty_api_key():
    cfg = LLMConfig(api_key="", oauth=_oauth())
    assert cfg.oauth is not None


def test_oauth_allows_placeholder_api_key():
    cfg = LLMConfig(api_key="YOUR_API_KEY", oauth=_oauth())
    assert cfg.oauth is not None


def test_nested_oauth_parses_from_dict():
    cfg = LLMConfig(
        api_key="",
        oauth={"token_url": "https://issuer/token", "client_id": "cid", "scopes": ["s1"]},
    )
    assert cfg.oauth.client_id == "cid"
    assert cfg.oauth.scopes == ["s1"]


# --- brand provider preset (#3) ------------------------------------------


def test_provider_preset_resolves_base_url_api_type_and_env_key(monkeypatch):
    from mote.contracts.config.model.llm import LLMType

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-real")
    cfg = LLMConfig(provider="deepseek")
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.api_type == LLMType.DEEPSEEK
    assert cfg.api_key == "sk-deepseek-real"


def test_provider_preset_user_values_win(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    cfg = LLMConfig(provider="deepseek", base_url="https://custom/v1", api_key="sk-mine")
    assert cfg.base_url == "https://custom/v1"
    assert cfg.api_key == "sk-mine"


def test_provider_anthropic_selects_native_wire():
    from mote.contracts.config.model.llm import LLMType

    cfg = LLMConfig(provider="anthropic", api_key="sk-ant")
    assert cfg.api_type == LLMType.ANTHROPIC


def test_provider_without_env_key_falls_back_to_default(monkeypatch):
    # No env key set: api_key stays the default placeholder ("sk-"), which the
    # legacy auth check still accepts, so construction succeeds (back-compat).
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = LLMConfig(provider="groq")
    assert cfg.base_url == "https://api.groq.com/openai/v1"


def test_no_provider_back_compat_unchanged():
    from mote.contracts.config.model.llm import LLMType

    cfg = LLMConfig(api_key="sk-x")
    assert cfg.provider is None
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_type == LLMType.OPENAI
