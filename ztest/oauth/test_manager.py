#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OAuthManager: refresh-on-expiry, caching, mtime re-read, force_refresh."""
from __future__ import annotations

import time

import pytest

from metagpt.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from metagpt.router.oauth.manager import OAuthManager
from metagpt.router.oauth.models import OAuthToken
from metagpt.router.oauth.storage.file_store import FileCredentialStore


class FakeClient:
    """Counts mint/refresh calls and returns tokens with a configurable TTL."""

    def __init__(self, ttl: float = 3600):
        self.ttl = ttl
        self.cc_calls = 0
        self.refresh_calls = 0

    def client_credentials(self) -> OAuthToken:
        self.cc_calls += 1
        return OAuthToken(access_token=f"cc-{self.cc_calls}", expires_at=time.time() + self.ttl)

    def refresh(self, refresh_token: str) -> OAuthToken:
        self.refresh_calls += 1
        return OAuthToken(
            access_token=f"rf-{self.refresh_calls}", refresh_token=refresh_token, expires_at=time.time() + self.ttl
        )


def _manager(tmp_path, client, **cfg_kw):
    cfg = OAuthProviderConfig(token_url="https://issuer/token", client_id="cid", **cfg_kw)
    store = FileCredentialStore("provm", base_dir=tmp_path)
    mgr = OAuthManager(cfg, provider="provm", store=store, client=client)
    # Keep the cross-process lock inside tmp_path for hermeticity.
    mgr._lock_path = tmp_path / "provm.lock"
    return mgr, store


def test_bootstrap_mints_via_client_credentials(tmp_path):
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)
    token = mgr.get_valid_token()
    assert token == "cc-1"
    assert client.cc_calls == 1
    # persisted
    assert store.load().access_token == "cc-1"


def test_cached_token_not_refetched(tmp_path):
    client = FakeClient()
    mgr, _ = _manager(tmp_path, client)
    assert mgr.get_valid_token() == "cc-1"
    assert mgr.get_valid_token() == "cc-1"  # served from cache
    assert client.cc_calls == 1


def test_expired_token_triggers_refresh(tmp_path):
    client = FakeClient(ttl=3600)
    mgr, store = _manager(tmp_path, client)
    # Seed an already-expired token with a refresh token.
    store.save(OAuthToken(access_token="old", refresh_token="r0", expires_at=time.time() - 10))
    token = mgr.get_valid_token()
    assert token == "rf-1"
    assert client.refresh_calls == 1


def test_buffer_triggers_proactive_refresh(tmp_path):
    client = FakeClient(ttl=3600)
    mgr, store = _manager(tmp_path, client, expiry_buffer_s=300)
    # Token valid for 100s but inside the 300s buffer => treated as expired.
    store.save(OAuthToken(access_token="soon", refresh_token="r0", expires_at=time.time() + 100))
    assert mgr.get_valid_token() == "rf-1"


def test_mtime_reread_skips_redundant_refresh(tmp_path):
    """A fresh token written to the store after our cache went stale is reused."""
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)

    # Prime cache with an expired token (cache only; store will be updated by
    # "another worker").
    mgr._cached = OAuthToken(access_token="stale", refresh_token="r0", expires_at=time.time() - 5)
    # Another process refreshed and persisted a valid token.
    store.save(OAuthToken(access_token="fresh-from-peer", expires_at=time.time() + 3600))

    token = mgr.get_valid_token()
    assert token == "fresh-from-peer"
    # No network refresh happened because the re-read under lock found it valid.
    assert client.cc_calls == 0
    assert client.refresh_calls == 0


def test_force_refresh_bypasses_buffer(tmp_path):
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)
    # A perfectly valid token exists...
    store.save(OAuthToken(access_token="valid", expires_at=time.time() + 9999))
    mgr._cached = store.load()
    # ...but force_refresh mints anyway.
    tok = mgr.force_refresh()
    assert tok is not None
    assert tok.access_token == "cc-1"
    assert client.cc_calls == 1


def test_force_refresh_returns_none_on_permanent_failure(tmp_path):
    from metagpt.router.oauth.errors import OAuthRefreshError

    class FailingClient(FakeClient):
        def client_credentials(self):
            raise OAuthRefreshError("nope", recoverable=False)

    mgr, _ = _manager(tmp_path, FailingClient())
    assert mgr.force_refresh() is None


def test_refresh_grant_without_token_errors(tmp_path):
    from metagpt.router.oauth.errors import OAuthConfigError

    client = FakeClient()
    mgr, _ = _manager(tmp_path, client, grant_type=GrantType.REFRESH_TOKEN)
    try:
        mgr.get_valid_token()
        assert False, "expected OAuthConfigError"
    except OAuthConfigError:
        pass


# --- interactive login dispatch (#4) -------------------------------------


def test_login_dispatches_device_code_and_persists(tmp_path, monkeypatch):
    import metagpt.router.oauth.flows as flows_mod

    captured = OAuthToken(access_token="logged-in", refresh_token="r-li", expires_at=time.time() + 3600)
    monkeypatch.setattr(flows_mod, "run_device_code_flow", lambda config, callbacks=None: captured)

    mgr, store = _manager(tmp_path, FakeClient(), grant_type=GrantType.DEVICE_CODE)
    tok = mgr.login()
    assert tok.access_token == "logged-in"
    # persisted + cached
    assert store.load().access_token == "logged-in"
    assert mgr.get_valid_token() == "logged-in"


def test_login_dispatches_authorization_code(tmp_path, monkeypatch):
    import metagpt.router.oauth.flows as flows_mod

    token = OAuthToken(access_token="ac-token", expires_at=time.time() + 3600)
    monkeypatch.setattr(flows_mod, "run_auth_code_flow", lambda config, callbacks=None: token)

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.AUTHORIZATION_CODE)
    assert mgr.login().access_token == "ac-token"


def test_login_rejects_headless_grant(tmp_path):
    from metagpt.router.oauth.errors import OAuthConfigError

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.CLIENT_CREDENTIALS)
    with pytest.raises(OAuthConfigError):
        mgr.login()


def test_interactive_grant_without_token_says_login_first(tmp_path):
    from metagpt.router.oauth.errors import OAuthConfigError

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.DEVICE_CODE)
    with pytest.raises(OAuthConfigError):
        mgr.get_valid_token()
