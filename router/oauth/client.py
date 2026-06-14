#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuthClient: token-endpoint interaction over sync ``httpx.Client``.

Synchronous on purpose: the two injection points in ``OpenAILLM``
(``_init_client`` / ``rotate_credential``) are sync and refreshes are
infrequent, so a sync client avoids awaiting inside sync code.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from metagpt.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from metagpt.common.logs import log_class
from metagpt.router.oauth.errors import (
    OAuthConfigError,
    classify_refresh_failure,
)
from metagpt.router.oauth.jwt_utils import JWTDecodeError, parse_claims
from metagpt.router.oauth.models import OAuthToken

_DEFAULT_TIMEOUT = 30.0


@log_class(level="DEBUG")
class OAuthClient:
    """Mint/refresh/revoke tokens against a provider's OAuth2 endpoints."""

    def __init__(self, config: OAuthProviderConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.config = config
        self._timeout = timeout

    # --- public grants -----------------------------------------------------

    def client_credentials(self) -> OAuthToken:
        """Obtain a token via the ``client_credentials`` grant (RFC 6749 §4.4)."""
        data = {
            "grant_type": GrantType.CLIENT_CREDENTIALS.value,
            "client_id": self.config.client_id,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        if self.config.scopes:
            data["scope"] = " ".join(self.config.scopes)
        if self.config.audience:
            data["audience"] = self.config.audience
        return self._token_request(data)

    def refresh(self, refresh_token: str) -> OAuthToken:
        """Exchange a ``refresh_token`` for a fresh access token (RFC 6749 §6)."""
        if not refresh_token:
            raise OAuthConfigError("refresh() requires a non-empty refresh_token")
        data = {
            "grant_type": GrantType.REFRESH_TOKEN.value,
            "refresh_token": refresh_token,
            "client_id": self.config.client_id,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        if self.config.scopes:
            data["scope"] = " ".join(self.config.scopes)
        token = self._token_request(data)
        # Carry the prior refresh token forward when the server doesn't rotate it.
        if not token.refresh_token:
            token.refresh_token = refresh_token
        return token

    def revoke(self, token: str, *, token_type_hint: Optional[str] = None) -> bool:
        """Best-effort RFC 7009 token revocation. Returns True on 2xx."""
        revoke_url = self.config.issuer
        # P1 has no dedicated revoke_url field; derive from token_url when the
        # issuer doesn't expose one. Callers in P1 generally won't use this.
        url = revoke_url or self.config.resolved_token_url().replace("/token", "/revoke")
        data = {"token": token, "client_id": self.config.client_id}
        if token_type_hint:
            data["token_type_hint"] = token_type_hint
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, data=data)
        return resp.is_success

    # --- internals ---------------------------------------------------------

    def _token_request(self, data: dict) -> OAuthToken:
        url = self.config.resolved_token_url()
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, data=data, headers={"Accept": "application/json"})
        if not resp.is_success:
            error_code, description = self._parse_error(resp)
            raise classify_refresh_failure(
                status_code=resp.status_code, error_code=error_code, description=description
            )
        return self._parse_token(resp)

    @staticmethod
    def _parse_error(resp: "httpx.Response") -> tuple[Optional[str], Optional[str]]:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return None, resp.text[:200] if resp.text else None
        if not isinstance(body, dict):
            return None, None
        return body.get("error"), body.get("error_description") or body.get("error")

    @staticmethod
    def _parse_token(resp: "httpx.Response") -> OAuthToken:
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise classify_refresh_failure(
                status_code=resp.status_code,
                error_code="invalid_response",
                description="token response missing access_token",
            )

        scopes: list[str] = []
        scope_str = body.get("scope")
        if isinstance(scope_str, str) and scope_str:
            scopes = scope_str.split()

        # Expiry: prefer explicit expires_in; else fall back to JWT exp claim.
        claims = None
        try:
            claims = parse_claims(access_token)
        except JWTDecodeError:
            claims = None  # opaque (non-JWT) token

        expires_at: Optional[float] = None
        expires_in = body.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = time.time() + float(expires_in)
        elif claims is not None and claims.exp is not None:
            expires_at = float(claims.exp)

        return OAuthToken(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            expires_at=expires_at,
            scopes=scopes,
            claims=claims,
        )
