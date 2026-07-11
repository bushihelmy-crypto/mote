#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for interactive OAuth login flows (auth-code loopback + device-code)."""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

import mote.router.oauth.client as client_mod
from mote.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from mote.router.oauth.errors import OAuthConfigError, OAuthRefreshError
from mote.router.oauth.flows import LoginCallbacks, run_auth_code_flow, run_device_code_flow


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSeqClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None):
        return self._responses.pop(0)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A urllib opener that never routes through an HTTP(S) proxy, so the loopback
# callback reaches the local server even when *_PROXY env vars are set.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --- authorization_code (loopback) ---------------------------------------


def test_run_auth_code_flow_success(monkeypatch):
    port = _free_port()
    cfg = OAuthProviderConfig(
        provider=None,
        token_url="https://issuer/token",
        authorize_url="https://issuer/authorize",
        redirect_uri=f"http://127.0.0.1:{port}/callback",
        client_id="cid",
        scopes=["a", "b"],
        grant_type=GrantType.AUTHORIZATION_CODE,
    )
    # Fake the token endpoint exchange.
    monkeypatch.setattr(
        client_mod.httpx,
        "Client",
        FakeSeqClient([FakeResponse(200, {"access_token": "final-at", "refresh_token": "r9"})]),
    )

    # When the flow surfaces the authorize URL, simulate the browser redirect
    # back to the loopback server with the matching state.
    def on_url(url: str):
        state = parse_qs(urlsplit(url).query)["state"][0]

        def hit():
            redirect = f"http://127.0.0.1:{port}/callback?code=the-code&state={state}"
            _NO_PROXY_OPENER.open(redirect, timeout=5).read()

        threading.Thread(target=hit, daemon=True).start()

    tok = run_auth_code_flow(cfg, LoginCallbacks(on_url=on_url), timeout=10)
    assert tok.access_token == "final-at"
    assert tok.refresh_token == "r9"


def test_run_auth_code_flow_state_mismatch(monkeypatch):
    port = _free_port()
    cfg = OAuthProviderConfig(
        token_url="https://issuer/token",
        authorize_url="https://issuer/authorize",
        redirect_uri=f"http://127.0.0.1:{port}/callback",
        client_id="cid",
        grant_type=GrantType.AUTHORIZATION_CODE,
    )

    def on_url(url: str):
        def hit():
            redirect = f"http://127.0.0.1:{port}/callback?code=c&state=WRONG"
            _NO_PROXY_OPENER.open(redirect, timeout=5).read()

        threading.Thread(target=hit, daemon=True).start()

    with pytest.raises(OAuthRefreshError):
        run_auth_code_flow(cfg, LoginCallbacks(on_url=on_url), timeout=10)


def test_run_auth_code_flow_requires_client_id():
    cfg = OAuthProviderConfig(
        token_url="https://issuer/token",
        authorize_url="https://issuer/authorize",
        client_id=None,
        grant_type=GrantType.AUTHORIZATION_CODE,
    )
    with pytest.raises(OAuthConfigError):
        run_auth_code_flow(cfg, timeout=1)


# --- device_code ----------------------------------------------------------


def test_run_device_code_flow_pending_then_success(monkeypatch):
    cfg = OAuthProviderConfig(
        token_url="https://issuer/token",
        device_authorization_url="https://issuer/device",
        client_id="cid",
        scopes=["read:user"],
        grant_type=GrantType.DEVICE_CODE,
    )
    monkeypatch.setattr(
        client_mod.httpx,
        "Client",
        FakeSeqClient(
            [
                FakeResponse(
                    200,
                    {
                        "device_code": "dc",
                        "user_code": "UC-1",
                        "verification_uri": "https://example/device",
                        "interval": 1,
                        "expires_in": 600,
                    },
                ),
                FakeResponse(400, {"error": "authorization_pending"}),
                FakeResponse(200, {"access_token": "dev-final"}),
            ]
        ),
    )
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)

    seen = {}
    tok = run_device_code_flow(cfg, LoginCallbacks(on_device_code=lambda info: seen.update({"code": info.user_code})))
    assert tok.access_token == "dev-final"
    assert seen["code"] == "UC-1"


def test_run_device_code_flow_requires_client_id():
    cfg = OAuthProviderConfig(
        token_url="https://issuer/token",
        device_authorization_url="https://issuer/device",
        client_id=None,
        grant_type=GrantType.DEVICE_CODE,
    )
    with pytest.raises(OAuthConfigError):
        run_device_code_flow(cfg)
