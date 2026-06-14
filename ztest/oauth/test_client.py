#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OAuthClient grants + failure classification (httpx monkeypatched).

``respx`` isn't installed in this environment, so we monkeypatch ``httpx.Client``
with a tiny fake that records the request and returns a canned response.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

import metagpt.router.oauth.client as client_mod
from metagpt.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from metagpt.router.oauth.client import OAuthClient
from metagpt.router.oauth.errors import OAuthRefreshError


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeClient:
    """Context-manager stand-in for httpx.Client capturing the last POST."""

    last_request = {}

    def __init__(self, response: FakeResponse):
        self._response = response

    def __call__(self, *args, **kwargs):  # httpx.Client(...) constructor
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None):
        FakeClient.last_request = {"url": url, "data": data or {}, "headers": headers or {}}
        return self._response


def _patch(monkeypatch, response: FakeResponse) -> FakeClient:
    fake = FakeClient(response)
    monkeypatch.setattr(client_mod.httpx, "Client", fake)
    return fake


def _cfg(**kw) -> OAuthProviderConfig:
    base = dict(token_url="https://issuer/token", client_id="cid")
    base.update(kw)
    return OAuthProviderConfig(**base)


def _jwt(payload: dict) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg':'none'})}.{b(payload)}.sig"


def test_client_credentials_success(monkeypatch):
    _patch(monkeypatch, FakeResponse(200, {"access_token": "at1", "expires_in": 3600, "scope": "a b"}))
    cfg = _cfg(scopes=["a", "b"], audience="aud1", client_secret="secret")
    tok = OAuthClient(cfg).client_credentials()

    assert tok.access_token == "at1"
    assert tok.scopes == ["a", "b"]
    assert tok.expires_at > time.time() + 3000
    sent = FakeClient.last_request["data"]
    assert sent["grant_type"] == GrantType.CLIENT_CREDENTIALS.value
    assert sent["scope"] == "a b"
    assert sent["audience"] == "aud1"
    assert sent["client_secret"] == "secret"


def test_refresh_success_carries_refresh_token(monkeypatch):
    # Server returns no new refresh_token -> client carries the old one forward.
    _patch(monkeypatch, FakeResponse(200, {"access_token": "at2", "expires_in": 60}))
    tok = OAuthClient(_cfg()).refresh("old-refresh")
    assert tok.access_token == "at2"
    assert tok.refresh_token == "old-refresh"
    assert FakeClient.last_request["data"]["grant_type"] == GrantType.REFRESH_TOKEN.value


def test_refresh_uses_new_rotated_token(monkeypatch):
    _patch(monkeypatch, FakeResponse(200, {"access_token": "at3", "refresh_token": "new-r"}))
    tok = OAuthClient(_cfg()).refresh("old-refresh")
    assert tok.refresh_token == "new-r"


def test_expiry_from_jwt_exp_when_no_expires_in(monkeypatch):
    exp = int(time.time()) + 500
    _patch(monkeypatch, FakeResponse(200, {"access_token": _jwt({"exp": exp, "email": "u@x"})}))
    tok = OAuthClient(_cfg()).client_credentials()
    assert tok.expires_at == float(exp)
    assert tok.claims is not None and tok.claims.email == "u@x"


def test_invalid_grant_is_unrecoverable(monkeypatch):
    _patch(monkeypatch, FakeResponse(400, {"error": "invalid_grant", "error_description": "expired"}))
    with pytest.raises(OAuthRefreshError) as ei:
        OAuthClient(_cfg()).refresh("dead")
    assert ei.value.recoverable is False
    assert ei.value.error_code == "invalid_grant"


def test_server_error_is_recoverable(monkeypatch):
    _patch(monkeypatch, FakeResponse(503, {"error": "temporarily_unavailable"}))
    with pytest.raises(OAuthRefreshError) as ei:
        OAuthClient(_cfg()).client_credentials()
    assert ei.value.recoverable is True


def test_missing_access_token_raises(monkeypatch):
    _patch(monkeypatch, FakeResponse(200, {"token_type": "bearer"}))
    with pytest.raises(OAuthRefreshError):
        OAuthClient(_cfg()).client_credentials()


def test_revoke_returns_success(monkeypatch):
    _patch(monkeypatch, FakeResponse(200, {}))
    assert OAuthClient(_cfg(issuer="https://issuer/revoke")).revoke("tok") is True
