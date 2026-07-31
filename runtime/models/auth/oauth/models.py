#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth runtime data models: tokens, claims, auth mode."""
from __future__ import annotations

import time
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class AuthMode(str, Enum):
    """How the LLM client is authenticating."""

    STATIC_KEY = "static_key"
    OAUTH = "oauth"  # token obtained/refreshed via OAuth runtime


class TokenClaims(BaseModel):
    """Selected claims parsed from a JWT access token (best-effort)."""

    email: Optional[str] = None
    account: Optional[str] = None
    exp: Optional[int] = None  # epoch seconds
    raw: Dict = Field(default_factory=dict)


class DeviceCodeInfo(BaseModel):
    """Device authorization response (RFC 8628 §3.2).

    Surfaced to the user (``user_code`` + ``verification_uri``) so they can
    authorize the login from a browser while the client polls the token endpoint.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str] = None
    interval: int = 5
    expires_in: Optional[int] = None


class OAuthToken(BaseModel):
    """A bearer access token plus refresh material and expiry metadata."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # epoch seconds; None => unknown/non-expiring
    scopes: List[str] = Field(default_factory=list)
    claims: Optional[TokenClaims] = None

    def is_expired(self, buffer: int = 0) -> bool:
        """True when the token is at/within ``buffer`` seconds of expiry.

        A token with no known ``expires_at`` is treated as non-expiring (False).
        """
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - max(0, buffer))
