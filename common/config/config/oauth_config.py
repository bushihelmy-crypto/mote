#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth provider configuration (pure data).

Lives beside ``llm_config.py`` (not under ``router/``) so ``LLMConfig`` can
reference ``OAuthProviderConfig`` without a ``common -> router`` import cycle.
The OAuth *runtime* (clients, manager, storage) lives in ``metagpt.router.oauth``.

This is opt-in: a provider only authenticates with OAuth when ``LLMConfig.oauth``
is set. When ``None``, the static ``api_key`` path is used unchanged.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, model_validator

from metagpt.common.utils.yaml_model import YamlModel


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
    # headers_extra) is filled from the registry in
    # ``metagpt.router.oauth.registry``. Explicit fields always win.
    provider: Optional[str] = Field(default=None, description="Provider preset name, e.g. 'openai' | 'anthropic'.")

    # Endpoints / identity
    issuer: Optional[str] = Field(default=None, description="OAuth issuer / authority base URL (informational).")
    token_url: Optional[str] = Field(
        default=None, description="OAuth2 token endpoint used for mint/refresh (or supplied via provider preset)."
    )
    authorize_url: Optional[str] = Field(
        default=None, description="Authorization endpoint for the interactive authorization_code flow."
    )
    device_authorization_url: Optional[str] = Field(
        default=None, description="Device authorization endpoint for the device_code flow (RFC 8628)."
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
        default=None, description="Pre-provisioned refresh token (used when grant_type=refresh_token)."
    )
    scopes: List[str] = Field(default_factory=list, description="Requested OAuth scopes.")
    audience: Optional[str] = Field(default=None, description="Optional 'audience' parameter for the token request.")

    # Storage / behavior
    store_backend: StoreBackend = Field(
        default=StoreBackend.FALLBACK, description="Where to persist tokens: file | keyring | fallback."
    )
    headers_extra: Dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers merged into the LLM client's default_headers."
    )
    expiry_buffer_s: int = Field(
        default=300, description="Proactively refresh this many seconds before token expiry."
    )

    # Test hook: name of an env var that, when set, overrides ``token_url``.
    token_url_env_override: Optional[str] = Field(
        default=None, description="Name of an env var whose value overrides token_url (test/staging hook)."
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_provider_preset(cls, values: Any) -> Any:
        """Fill public endpoint metadata from a provider preset (user wins).

        Lazy-imports the registry (which lives under ``router/``) so there is no
        module-level ``common -> router`` import cycle.
        """
        if not isinstance(values, dict) or not values.get("provider"):
            return values
        from metagpt.router.oauth.registry import apply_preset

        return apply_preset(values)

    @model_validator(mode="after")
    def _require_token_url(self):
        """A token endpoint must be resolvable (set directly or via preset)."""
        if not self.token_url:
            raise ValueError(
                "OAuth config needs a 'token_url' (set it directly or use a known 'provider' preset)."
            )
        return self

    def resolved_token_url(self) -> str:
        """Return ``token_url``, honoring the env override hook when present."""
        import os

        if self.token_url_env_override:
            return os.environ.get(self.token_url_env_override) or self.token_url
        return self.token_url
