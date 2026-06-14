#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider preset registry for OAuth-authenticated, OpenAI-compatible providers.

A *preset* fills in the **public, provider-specific endpoint metadata** (issuer,
token URL, default scopes, extra headers, default grant) so a user only has to
supply their own ``client_id`` (+ secret / refresh_token). Presets deliberately
DO NOT include a ``client_id``: the hardcoded client IDs shipped in Codex /
Claude Code identify *those* CLIs, and reusing them would impersonate them
against the provider's OAuth server. Bring your own client.

Values are sourced from the public OAuth endpoints used by Codex
(``codex-rs/login``) and Claude Code (``src/constants/oauth.ts``,
``services/api/openai/chatgptAuth.ts``). Each preset names an env var that, when
set, overrides ``token_url`` (staging/testing hook), mirroring those tools'
prod/staging/local switch.
"""
from __future__ import annotations

import copy
from typing import Dict, List

from metagpt.common.config.config.oauth_config import GrantType

# name -> preset of OAuthProviderConfig fields (NO client_id / client_secret).
PROVIDER_PRESETS: Dict[str, dict] = {
    "openai": {
        "issuer": "https://auth.openai.com",
        "token_url": "https://auth.openai.com/oauth/token",
        "grant_type": GrantType.REFRESH_TOKEN.value,
        "scopes": ["openid", "profile", "email", "offline_access"],
        "headers_extra": {},
        "token_url_env_override": "METAGPT_OAUTH_OPENAI_TOKEN_URL",
    },
    "anthropic": {
        "issuer": "https://platform.claude.com",
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "grant_type": GrantType.REFRESH_TOKEN.value,
        "scopes": ["user:profile", "user:inference"],
        # Claude's OAuth bearer requires this beta opt-in header.
        "headers_extra": {"anthropic-beta": "oauth-2025-04-20"},
        "token_url_env_override": "METAGPT_OAUTH_ANTHROPIC_TOKEN_URL",
    },
}

# Fields that should be merged (preset base + user overrides) rather than simply
# filled-if-missing, so a user can add headers without dropping the beta header.
_MERGE_FIELDS = {"headers_extra"}


def list_presets() -> List[str]:
    """Return the registered provider preset names."""
    return sorted(PROVIDER_PRESETS)


def get_preset(name: str) -> dict:
    """Return a deep copy of the preset for ``name``.

    Raises ``KeyError`` (with the list of known providers) when unknown.
    """
    key = (name or "").strip().lower()
    if key not in PROVIDER_PRESETS:
        raise KeyError(f"unknown OAuth provider preset {name!r}; known: {list_presets()}")
    return copy.deepcopy(PROVIDER_PRESETS[key])


def apply_preset(values: dict) -> dict:
    """Merge a provider preset into a raw config ``values`` dict (user wins).

    No-op when ``values`` has no ``provider`` key. For scalar/list fields the
    preset only fills values the user left empty; ``headers_extra`` is merged so
    user headers add to (not replace) the preset's. Returns ``values`` mutated
    in place for convenience.
    """
    provider = values.get("provider")
    if not provider:
        return values

    preset = get_preset(provider)
    for field, preset_value in preset.items():
        if field in _MERGE_FIELDS:
            user_value = values.get(field) or {}
            values[field] = {**preset_value, **user_value}
        elif values.get(field) in (None, [], {}, ""):
            values[field] = preset_value
    return values


__all__ = ["PROVIDER_PRESETS", "list_presets", "get_preset", "apply_preset"]
