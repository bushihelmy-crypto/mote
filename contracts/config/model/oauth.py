#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth provider configuration (pure data).

Lives beside ``llm_config.py`` (not under ``router/``) so ``LLMConfig`` can
reference ``OAuthProviderConfig`` without a cross-layer import cycle.
The OAuth runtime (clients, manager, storage) lives in ``mote.runtime.models.auth.oauth``.

This is opt-in: a provider only authenticates with OAuth when ``LLMConfig.oauth``
is set. When ``None``, the static ``api_key`` path is used unchanged.
"""

from __future__ import annotations

import copy
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel


class GrantType(str, Enum):
    """OAuth2 grant types.

    ``CLIENT_CREDENTIALS`` / ``REFRESH_TOKEN`` are headless (no user
    interaction). ``AUTHORIZATION_CODE`` (PKCE, loopback redirect) and
    ``DEVICE_CODE`` (RFC 8628) are interactive login flows that mint the first
    token; subsequent refreshes use the stored ``refresh_token``.
    """

    CLIENT_CREDENTIALS = "client_credentials"
    REFRESH_TOKEN = "refresh_token"
    AUTHORIZATION_CODE = "authorization_code"
    DEVICE_CODE = "device_code"


class StoreBackend(str, Enum):
    """Credential storage backend selector."""

    FILE = "file"
    KEYRING = "keyring"
    FALLBACK = "fallback"


class OAuthProviderConfig(YamlModel):
    """Declarative OAuth settings for a single provider.

    Supports both headless grants (``client_credentials`` / ``refresh_token``)
    and interactive login flows (``authorization_code`` with PKCE, ``device_code``
    per RFC 8628). Interactive flows mint the first token via
    ``OAuthManager.login``; subsequent calls refresh from the stored token.
    """

    # Optional provider preset: when set, public endpoint metadata (issuer,
    # token_url, authorize_url, device_authorization_url, scopes, grant_type,
    # headers_extra) is filled from the preset registry co-located below
    # (``PROVIDER_PRESETS``). Explicit fields always win.
    provider: Optional[str] = Field(default=None, description="Provider preset name, e.g. 'openai' | 'anthropic'.")

    # Endpoints / identity
    issuer: Optional[str] = Field(default=None, description="OAuth issuer / authority base URL (informational).")
    token_url: Optional[str] = Field(
        default=None,
        description="OAuth2 token endpoint used for mint/refresh (or supplied via provider preset).",
    )
    authorize_url: Optional[str] = Field(
        default=None,
        description="Authorization endpoint for the interactive authorization_code flow.",
    )
    device_authorization_url: Optional[str] = Field(
        default=None,
        description="Device authorization endpoint for the device_code flow (RFC 8628).",
    )
    redirect_uri: str = Field(
        default="http://localhost:53692/callback",
        description="Loopback redirect URI for the authorization_code flow.",
    )
    # ``client_id`` is an ordinary optional field defaulting to None (bring your
    # own). Out-of-box login for a vendor only happens when someone fills the
    # public PKCE client_id (config/env). The requirement is enforced at
    # flow-time, not config-time, so presets stay constructible without it.
    client_id: Optional[str] = Field(default=None, description="OAuth client identifier (BYO; None by default).")
    client_secret: Optional[str] = Field(default=None, description="OAuth client secret (confidential clients).")

    # Grant inputs
    grant_type: GrantType = Field(
        default=GrantType.CLIENT_CREDENTIALS,
        description="Grant used to obtain the first token (headless or interactive).",
    )
    refresh_token: Optional[str] = Field(
        default=None,
        description="Pre-provisioned refresh token (used when grant_type=refresh_token).",
    )
    scopes: List[str] = Field(default_factory=list, description="Requested OAuth scopes.")
    audience: Optional[str] = Field(default=None, description="Optional 'audience' parameter for the token request.")

    # Storage / behavior
    store_backend: StoreBackend = Field(
        default=StoreBackend.FALLBACK,
        description="Where to persist tokens: file | keyring | fallback.",
    )
    storage_root: Optional[Path] = Field(default=None, exclude=True)
    headers_extra: Dict[str, str] = Field(
        default_factory=dict,
        description="Extra HTTP headers merged into the LLM client's default_headers.",
    )
    expiry_buffer_s: int = Field(
        default=300,
        description="Proactively refresh this many seconds before token expiry.",
    )

    # Test hook: name of an env var that, when set, overrides ``token_url``.
    token_url_env_override: Optional[str] = Field(
        default=None,
        description="Name of an env var whose value overrides token_url (test/staging hook).",
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_provider_preset(cls, values: Any) -> Any:
        """Fill public endpoint metadata from a provider preset (user wins).

        Uses the module-level preset registry (co-located below) so there is no
        cross-layer import cycle.
        """
        if not isinstance(values, dict) or not values.get("provider"):
            return values
        return apply_preset(values)

    @model_validator(mode="after")
    def _require_token_url(self):
        """A token endpoint must be resolvable (set directly or via preset)."""
        if not self.token_url:
            raise ValueError("OAuth config needs a 'token_url' (set it directly or use a known 'provider' preset).")
        return self


# ---------------------------------------------------------------------------
# Provider preset registry for OAuth-authenticated providers.
#
# A *preset* fills in the **public, provider-specific endpoint metadata** (issuer,
# token URL, authorize/device endpoints, default scopes, extra headers, default
# grant) so a user only has to supply a ``client_id`` (+ secret / refresh_token).
#
# ``client_id`` is an ordinary optional config field (default ``None``): presets
# deliberately DO NOT ship one, because the hardcoded client IDs in Codex / Claude
# Code identify *those* CLIs and reusing them would impersonate them. Out-of-box
# login only happens when someone fills the public PKCE ``client_id`` themselves
# (config/env). The requirement is enforced at flow-time, not config-time.
#
# Co-located with :class:`OAuthProviderConfig` (rather than under ``router/``) so
# the ``@model_validator`` above can apply presets without a cross-layer import
# cycle. This module is the authoritative preset owner.
# ---------------------------------------------------------------------------

# name -> preset of OAuthProviderConfig fields (NO client_id / client_secret).
PROVIDER_PRESETS: Dict[str, dict] = {
    "openai": {
        "issuer": "https://auth.openai.com",
        "token_url": "https://auth.openai.com/oauth/token",
        "authorize_url": "https://auth.openai.com/oauth/authorize",
        "grant_type": GrantType.REFRESH_TOKEN.value,
        "scopes": ["openid", "profile", "email", "offline_access"],
        "headers_extra": {},
        "token_url_env_override": "MOTE_OAUTH_OPENAI_TOKEN_URL",
    },
    "anthropic": {
        "issuer": "https://platform.claude.com",
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "authorize_url": "https://claude.ai/oauth/authorize",
        "grant_type": GrantType.REFRESH_TOKEN.value,
        "scopes": ["user:profile", "user:inference"],
        # Claude's OAuth bearer requires this beta opt-in header.
        "headers_extra": {"anthropic-beta": "oauth-2025-04-20"},
        "token_url_env_override": "MOTE_OAUTH_ANTHROPIC_TOKEN_URL",
    },
    # GitHub Copilot logs in via the OAuth 2.0 device flow (RFC 8628): no
    # loopback redirect, the user enters a code at a verification URL.
    "github-copilot": {
        "issuer": "https://github.com",
        "token_url": "https://github.com/login/oauth/access_token",
        "device_authorization_url": "https://github.com/login/device/code",
        "grant_type": GrantType.DEVICE_CODE.value,
        "scopes": ["read:user"],
        "headers_extra": {},
        "token_url_env_override": "MOTE_OAUTH_GITHUB_COPILOT_TOKEN_URL",
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
