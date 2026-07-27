#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth runtime for OpenAI-compatible LLM providers (P1, headless).

Public surface:
- :class:`OAuthManager` — serve/refresh a valid bearer token (used by OpenAILLM).
- :class:`OAuthClient` — sync token-endpoint client (mint/refresh/revoke).
- :class:`OAuthToken` / :class:`TokenClaims` / :class:`AuthMode` — data models.
- :class:`OAuthError` & friends — error hierarchy.
"""
from __future__ import annotations

from mote.runtime.models.auth.oauth.client import OAuthClient
from mote.runtime.models.auth.oauth.errors import (
    OAuthConfigError,
    OAuthError,
    OAuthHTTPError,
    OAuthRefreshError,
    classify_refresh_failure,
)
from mote.runtime.models.auth.oauth.flows import LoginCallbacks, run_auth_code_flow, run_device_code_flow
from mote.runtime.models.auth.oauth.manager import OAuthManager
from mote.runtime.models.auth.oauth.models import AuthMode, DeviceCodeInfo, OAuthToken, TokenClaims
from mote.runtime.models.auth.oauth.registry import PROVIDER_PRESETS, get_preset, list_presets

__all__ = [
    "OAuthManager",
    "OAuthClient",
    "OAuthToken",
    "TokenClaims",
    "DeviceCodeInfo",
    "AuthMode",
    "OAuthError",
    "OAuthConfigError",
    "OAuthHTTPError",
    "OAuthRefreshError",
    "classify_refresh_failure",
    "PROVIDER_PRESETS",
    "get_preset",
    "list_presets",
    "LoginCallbacks",
    "run_auth_code_flow",
    "run_device_code_flow",
]
