#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the LLM provider catalog: preset fill, user-wins, env-key discovery."""
from __future__ import annotations

import pytest

from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.router.llm.provider_catalog import (
    PROVIDER_CATALOG,
    apply_provider_preset,
    detect_provider,
    find_env_keys,
    get_env_api_key,
    get_provider_preset,
    list_providers,
)


def test_list_providers_includes_known_brands():
    names = list_providers()
    for brand in ("openai", "anthropic", "deepseek", "groq", "fireworks"):
        assert brand in names


def test_get_provider_preset_deepseek():
    preset = get_provider_preset("deepseek")
    assert preset.base_url == "https://api.deepseek.com/v1"
    assert preset.api_type == LLMType.DEEPSEEK
    assert preset.env_keys == ["DEEPSEEK_API_KEY"]


def test_get_provider_preset_is_case_insensitive():
    assert get_provider_preset("DeepSeek").base_url == "https://api.deepseek.com/v1"


def test_anthropic_uses_native_wire():
    assert get_provider_preset("anthropic").api_type == LLMType.ANTHROPIC


def test_openai_compatible_brand_uses_openai_wire():
    # A brand without a dedicated LLMType falls back to the OpenAI wire.
    assert get_provider_preset("groq").api_type == LLMType.OPENAI


def test_get_provider_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_provider_preset("does-not-exist")


def test_apply_preset_noop_without_provider():
    values = {"base_url": "https://x/v1", "api_key": "k"}
    assert apply_provider_preset(dict(values)) == values


def test_apply_preset_fills_base_url_and_api_type():
    out = apply_provider_preset({"provider": "moonshot"})
    assert out["base_url"] == "https://api.moonshot.cn/v1"
    assert out["api_type"] == LLMType.MOONSHOT


def test_apply_preset_user_base_url_wins():
    out = apply_provider_preset({"provider": "moonshot", "base_url": "https://my/v1"})
    assert out["base_url"] == "https://my/v1"


def test_apply_preset_links_oauth_provider_when_unnamed():
    out = apply_provider_preset({"provider": "anthropic", "oauth": {"client_id": "c"}})
    assert out["oauth"]["provider"] == "anthropic"


def test_apply_preset_does_not_override_explicit_oauth_provider():
    out = apply_provider_preset({"provider": "anthropic", "oauth": {"provider": "custom"}})
    assert out["oauth"]["provider"] == "custom"


# --- env-key discovery ----------------------------------------------------


def test_find_env_keys_returns_set_vars(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    assert find_env_keys("deepseek") == ["DEEPSEEK_API_KEY"]


def test_find_env_keys_none_when_unset(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert find_env_keys("deepseek") is None


def test_find_env_keys_unknown_provider_is_none():
    assert find_env_keys("nope") is None


def test_get_env_api_key_returns_first_set_value(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-1")
    assert get_env_api_key("groq") == "gsk-1"


def test_get_env_api_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert get_env_api_key("groq") is None


def test_every_preset_has_env_keys():
    for name, preset in PROVIDER_CATALOG.items():
        assert preset.env_keys, f"{name} has no env_keys"


# --- provider: auto detection --------------------------------------------


def test_detect_provider_by_base_url_host():
    assert detect_provider({"base_url": "https://api.deepseek.com/v1"}, environ={}) == "deepseek"


def test_detect_provider_by_base_url_subdomain():
    assert detect_provider({"base_url": "https://eu.api.openai.com/v1"}, environ={}) == "openai"


def test_detect_provider_by_model_hint():
    assert detect_provider({"model": "claude-opus-4-6"}, environ={}) == "anthropic"


def test_detect_provider_by_env_key():
    assert detect_provider({}, environ={"GROQ_API_KEY": "gsk-1"}) == "groq"


def test_detect_provider_base_url_beats_model_and_env():
    values = {"base_url": "https://api.deepseek.com/v1", "model": "claude-opus"}
    assert detect_provider(values, environ={"GROQ_API_KEY": "x"}) == "deepseek"


def test_detect_provider_model_beats_env():
    assert detect_provider({"model": "claude-opus"}, environ={"GROQ_API_KEY": "x"}) == "anthropic"


def test_detect_provider_none_when_no_signal():
    assert detect_provider({}, environ={}) is None


def test_llmconfig_auto_resolves_via_base_url():
    cfg = LLMConfig(provider="auto", base_url="https://api.deepseek.com/v1", api_key="sk-x")
    assert cfg.provider == "deepseek"
    assert cfg.api_type == LLMType.DEEPSEEK


def test_llmconfig_auto_resolves_via_model_hint():
    cfg = LLMConfig(provider="auto", model="claude-opus-4-6", api_key="sk-x")
    assert cfg.provider == "anthropic"
    assert cfg.api_type == LLMType.ANTHROPIC
    assert cfg.base_url == "https://api.anthropic.com"


def test_llmconfig_auto_fills_api_key_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    cfg = LLMConfig(provider="auto", base_url="https://api.deepseek.com/v1")
    assert cfg.provider == "deepseek"
    assert cfg.api_key == "sk-from-env"


def test_llmconfig_auto_falls_back_to_default_when_undetectable(monkeypatch):
    # No recognisable signal (no base_url/model, and no brand env key present):
    # provider normalises to None and the plain defaults hold.
    for preset in PROVIDER_CATALOG.values():
        for env_key in preset.env_keys:
            monkeypatch.delenv(env_key, raising=False)
    cfg = LLMConfig(provider="auto", api_key="sk-x")
    assert cfg.provider is None
    assert cfg.api_type == LLMType.OPENAI
