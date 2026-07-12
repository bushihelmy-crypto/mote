#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the provider preset registry + its wiring into OAuthProviderConfig."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from mote.router.oauth.registry import apply_preset, get_preset, list_presets


def test_list_presets_includes_known_providers():
    names = list_presets()
    assert "openai" in names
    assert "anthropic" in names


def test_get_preset_openai_values():
    preset = get_preset("openai")
    assert preset["token_url"] == "https://auth.openai.com/oauth/token"
    assert preset["grant_type"] == GrantType.REFRESH_TOKEN.value
    assert "offline_access" in preset["scopes"]


def test_get_preset_anthropic_has_beta_header():
    preset = get_preset("anthropic")
    assert preset["headers_extra"]["anthropic-beta"] == "oauth-2025-04-20"


def test_get_preset_is_case_insensitive_and_copied():
    a = get_preset("OpenAI")
    a["scopes"].append("mutated")
    # mutation of the returned copy must not leak into the registry
    assert "mutated" not in get_preset("openai")["scopes"]


def test_get_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("does-not-exist")


def test_preset_never_includes_client_id():
    for name in list_presets():
        preset = get_preset(name)
        assert "client_id" not in preset
        assert "client_secret" not in preset


def test_apply_preset_noop_without_provider():
    values = {"token_url": "https://x/token", "client_id": "c"}
    assert apply_preset(dict(values)) == values


# --- config wiring -------------------------------------------------------


def test_config_fills_endpoints_from_preset():
    cfg = OAuthProviderConfig(provider="anthropic", client_id="mycid")
    assert cfg.token_url == "https://platform.claude.com/v1/oauth/token"
    assert cfg.grant_type == GrantType.REFRESH_TOKEN
    assert cfg.scopes == ["user:profile", "user:inference"]
    assert cfg.headers_extra["anthropic-beta"] == "oauth-2025-04-20"


def test_config_user_fields_win_over_preset():
    cfg = OAuthProviderConfig(
        provider="openai",
        client_id="x",
        token_url="https://my/override/token",
        scopes=["custom"],
    )
    assert cfg.token_url == "https://my/override/token"
    assert cfg.scopes == ["custom"]


def test_config_headers_merge_keeps_preset_and_user():
    cfg = OAuthProviderConfig(provider="anthropic", client_id="x", headers_extra={"X-Org": "acme"})
    assert cfg.headers_extra == {"anthropic-beta": "oauth-2025-04-20", "X-Org": "acme"}


def test_config_client_id_optional_with_preset():
    # client_id is now an ordinary optional field (BYO): a preset stays
    # constructible without one; the requirement is enforced at flow-time.
    cfg = OAuthProviderConfig(provider="openai")
    assert cfg.client_id is None
    assert cfg.token_url == "https://auth.openai.com/oauth/token"


def test_config_requires_token_url_without_preset():
    with pytest.raises(ValidationError):
        OAuthProviderConfig(client_id="x")


def test_config_unknown_provider_raises():
    with pytest.raises(KeyError):
        OAuthProviderConfig(provider="nope", client_id="x")


def test_config_direct_token_url_still_works_without_provider():
    cfg = OAuthProviderConfig(token_url="https://issuer/token", client_id="x")
    assert cfg.provider is None
    assert cfg.token_url == "https://issuer/token"
