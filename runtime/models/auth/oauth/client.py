#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuthClient: token-endpoint interaction over sync ``httpx.Client``.

Synchronous on purpose: provider construction and Product-controlled refresh-slot
activation are sync seams, and refreshes are infrequent.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from mote.contracts.config.model.oauth import GrantType, OAuthProviderConfig
from mote.runtime.models.auth.oauth.errors import OAuthConfigError, OAuthRefreshError, classify_refresh_failure
from mote.runtime.models.auth.oauth.jwt_utils import JWTDecodeError, parse_claims
from mote.runtime.models.auth.oauth.models import DeviceCodeInfo, OAuthToken
from mote.runtime.telemetry.logging import log_class

_DEFAULT_TIMEOUT = 30.0
_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEVICE_FLOW_DEFAULT_EXPIRY = 900  # seconds; fallback when server omits expires_in


def _resolved_token_url(config: OAuthProviderConfig) -> str:
    token_url = config.token_url or ""
    if config.token_url_env_override:
        return os.environ.get(config.token_url_env_override) or token_url
    return token_url


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

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> OAuthToken:
        """Exchange an authorization ``code`` for a token (RFC 6749 §4.1.3 + PKCE)."""
        if not code:
            raise OAuthConfigError("exchange_code() requires a non-empty authorization code")
        self._require_client_id()
        data = {
            "grant_type": GrantType.AUTHORIZATION_CODE.value,
            "code": code,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        return self._token_request(data)

    def request_device_code(self) -> DeviceCodeInfo:
        """Start the device flow: request a device + user code (RFC 8628 §3.1)."""
        self._require_client_id()
        url = self.config.device_authorization_url
        if not url:
            raise OAuthConfigError("device_code flow requires a 'device_authorization_url'")
        data = {"client_id": self.config.client_id}
        if self.config.scopes:
            data["scope"] = " ".join(self.config.scopes)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, data=data, headers={"Accept": "application/json"})
        if not resp.is_success:
            error_code, description = self._parse_error(resp)
            raise classify_refresh_failure(
                status_code=resp.status_code,
                error_code=error_code,
                description=description,
            )
        return self._parse_device_code(resp)

    def poll_device_token(self, device_code: str, *, interval: int = 5, expires_in: Optional[int] = None) -> OAuthToken:
        """Poll the token endpoint until the user authorizes (RFC 8628 §3.4).

        Handles ``authorization_pending`` (keep waiting) and ``slow_down``
        (back off by 5s). Raises :class:`OAuthRefreshError` on expiry or a
        terminal error code.
        """
        self._require_client_id()
        window = expires_in if expires_in is not None else _DEVICE_FLOW_DEFAULT_EXPIRY
        deadline = time.time() + window
        delay = max(1, int(interval or 5))
        token_url = _resolved_token_url(self.config)
        while True:
            if time.time() >= deadline:
                raise OAuthRefreshError(
                    "device authorization expired before approval",
                    error_code="expired_token",
                    recoverable=False,
                )
            time.sleep(delay)
            data = {
                "grant_type": _DEVICE_CODE_GRANT,
                "device_code": device_code,
                "client_id": self.config.client_id,
            }
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(token_url, data=data, headers={"Accept": "application/json"})
            if resp.is_success:
                return self._parse_token(resp)
            error_code, description = self._parse_error(resp)
            if error_code == "authorization_pending":
                continue
            if error_code == "slow_down":
                delay += 5
                continue
            raise classify_refresh_failure(
                status_code=resp.status_code,
                error_code=error_code,
                description=description,
            )

    def revoke(self, token: str, *, token_type_hint: Optional[str] = None) -> bool:
        """Best-effort RFC 7009 token revocation. Returns True on 2xx."""
        revoke_url = self.config.issuer
        # P1 has no dedicated revoke_url field; derive from token_url when the
        # issuer doesn't expose one. Callers in P1 generally won't use this.
        url = revoke_url or _resolved_token_url(self.config).replace("/token", "/revoke")
        data = {"token": token, "client_id": self.config.client_id}
        if token_type_hint:
            data["token_type_hint"] = token_type_hint
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, data=data)
        return resp.is_success

    # --- internals ---------------------------------------------------------

    def _require_client_id(self) -> None:
        """Enforce the flow-time ``client_id`` requirement (None by default).

        Login flows can't proceed without a client identifier; presets ship
        without one so a vendor's CLI isn't impersonated. Surface a clear error
        telling the user to bring their own.
        """
        if not self.config.client_id:
            raise OAuthConfigError(
                "this OAuth flow requires a 'client_id'; set it in config or via env (bring your own)"
            )

    @staticmethod
    def _parse_device_code(resp: "httpx.Response") -> DeviceCodeInfo:
        body = resp.json()
        device_code = body.get("device_code")
        user_code = body.get("user_code")
        verification_uri = body.get("verification_uri") or body.get("verification_url")
        if not (device_code and user_code and verification_uri):
            raise classify_refresh_failure(
                status_code=resp.status_code,
                error_code="invalid_response",
                description="device authorization response missing required fields",
            )
        interval = body.get("interval")
        return DeviceCodeInfo(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=body.get("verification_uri_complete"),
            interval=int(interval) if isinstance(interval, (int, float)) else 5,
            expires_in=body.get("expires_in"),
        )

    def _token_request(self, data: dict) -> OAuthToken:
        url = _resolved_token_url(self.config)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, data=data, headers={"Accept": "application/json"})
        if not resp.is_success:
            error_code, description = self._parse_error(resp)
            raise classify_refresh_failure(
                status_code=resp.status_code,
                error_code=error_code,
                description=description,
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
