#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OAuthManager: refresh-on-expiry, caching, mtime re-read, force_refresh."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.config.model.oauth import GrantType, OAuthProviderConfig
from mote.runtime.models.auth.oauth.manager import OAuthManager
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialAction,
    CredentialCommand,
    CredentialCommandDisposition,
    CredentialState,
    CredentialUse,
)
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore


def _publish(store: FileCredentialStore, token: OAuthToken) -> None:
    current = store.load_metadata()
    store.publish(token, expected_revision=0 if current is None else current.revision)


def _borrow_token(store: FileCredentialStore) -> OAuthToken:
    borrowed = store.borrow(
        CredentialUse(store.external_name, "test-account", (), "test-consumer"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert borrowed is not None
    try:
        return borrowed.token
    finally:
        store.release_borrow(borrowed)


def _access(manager: OAuthManager) -> str:
    borrowed = manager.acquire_valid_borrow(expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    try:
        return borrowed.token.access_token
    finally:
        manager.release_borrow(borrowed)


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
            access_token=f"rf-{self.refresh_calls}",
            refresh_token=refresh_token,
            expires_at=time.time() + self.ttl,
        )

    def revoke(self, token: str, *, token_type_hint=None) -> bool:
        return bool(token)


def _manager(tmp_path, client, **cfg_kw):
    cfg = OAuthProviderConfig(token_url="https://issuer/token", client_id="cid", **cfg_kw)
    store = FileCredentialStore("provm", base_dir=tmp_path)
    mgr = OAuthManager(cfg, provider="provm", consumer_id="test-manager", store=store, client=client)
    # Keep the cross-process lock inside tmp_path for hermeticity.
    mgr._lock_path = tmp_path / "provm.lock"
    return mgr, store


def test_bootstrap_mints_via_client_credentials(tmp_path):
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)
    token = _access(mgr)
    assert token == "cc-1"
    assert client.cc_calls == 1
    # persisted
    assert _borrow_token(store).access_token == "cc-1"


def test_untrusted_provider_name_cannot_escape_lock_root(tmp_path):
    cfg = OAuthProviderConfig(token_url="https://issuer/token", client_id="cid")
    store = FileCredentialStore("../../outside", base_dir=tmp_path)
    manager = OAuthManager(cfg, provider="../../outside", consumer_id="test-manager", store=store, client=FakeClient())

    assert manager._lock_path.parent == tmp_path.resolve()
    assert "outside" not in manager._lock_path.name


def test_cached_token_not_refetched(tmp_path):
    client = FakeClient()
    mgr, _ = _manager(tmp_path, client)
    assert _access(mgr) == "cc-1"
    assert _access(mgr) == "cc-1"
    assert client.cc_calls == 1


def test_expired_token_triggers_refresh(tmp_path):
    client = FakeClient(ttl=3600)
    mgr, store = _manager(tmp_path, client)
    # Seed an already-expired token with a refresh token.
    _publish(store, OAuthToken(access_token="old", refresh_token="r0", expires_at=time.time() - 10))
    token = _access(mgr)
    assert token == "rf-1"
    assert client.refresh_calls == 1


def test_buffer_triggers_proactive_refresh(tmp_path):
    client = FakeClient(ttl=3600)
    mgr, store = _manager(tmp_path, client, expiry_buffer_s=300)
    # Token valid for 100s but inside the 300s buffer => treated as expired.
    _publish(store, OAuthToken(access_token="soon", refresh_token="r0", expires_at=time.time() + 100))
    assert _access(mgr) == "rf-1"


def test_mtime_reread_skips_redundant_refresh(tmp_path):
    """A fresh token written to the store after our cache went stale is reused."""
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)

    # Prime cache with an expired token (cache only; store will be updated by
    # "another worker").
    mgr._cached = OAuthToken(access_token="stale", refresh_token="r0", expires_at=time.time() - 5)
    # Another process refreshed and persisted a valid token.
    _publish(store, OAuthToken(access_token="fresh-from-peer", expires_at=time.time() + 3600))

    token = _access(mgr)
    assert token == "fresh-from-peer"
    # No network refresh happened because the re-read under lock found it valid.
    assert client.cc_calls == 0
    assert client.refresh_calls == 0


def test_two_managers_serialize_refresh_and_commit_one_generation(tmp_path):
    class SlowClient(FakeClient):
        def refresh(self, refresh_token: str) -> OAuthToken:
            time.sleep(0.05)
            return super().refresh(refresh_token)

    client = SlowClient()
    first, store = _manager(tmp_path, client)
    second, _ = _manager(tmp_path, client)
    _publish(
        store,
        OAuthToken(
            access_token="expired",
            refresh_token="refresh",
            expires_at=time.time() - 1,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_access, (first, second)))

    assert results == ["rf-1", "rf-1"]
    assert client.refresh_calls == 1
    record = store.load_metadata()
    assert record is not None
    assert record.revision == 2
    assert record.secret_generation == 2


def test_force_refresh_bypasses_buffer(tmp_path):
    client = FakeClient()
    mgr, store = _manager(tmp_path, client)
    # A perfectly valid token exists...
    _publish(store, OAuthToken(access_token="valid", expires_at=time.time() + 9999))
    mgr._cached = _borrow_token(store)
    # ...but force_refresh mints anyway.
    tok = mgr.force_refresh()
    assert tok is not None
    assert tok.access_token == "cc-1"
    assert client.cc_calls == 1


def test_force_refresh_returns_none_on_permanent_failure(tmp_path):
    from mote.runtime.models.auth.oauth.errors import OAuthRefreshError

    class FailingClient(FakeClient):
        def client_credentials(self):
            raise OAuthRefreshError("nope", recoverable=False)

    mgr, _ = _manager(tmp_path, FailingClient())
    assert mgr.force_refresh() is None


def test_refresh_grant_without_token_errors(tmp_path):
    from mote.runtime.models.auth.oauth.errors import OAuthConfigError

    client = FakeClient()
    mgr, _ = _manager(tmp_path, client, grant_type=GrantType.REFRESH_TOKEN)
    try:
        _access(mgr)
        assert False, "expected OAuthConfigError"
    except OAuthConfigError:
        pass


# --- interactive login dispatch (#4) -------------------------------------


def test_login_dispatches_device_code_and_persists(tmp_path, monkeypatch):
    import mote.runtime.models.auth.oauth.manager as manager_mod

    captured = OAuthToken(access_token="logged-in", refresh_token="r-li", expires_at=time.time() + 3600)
    monkeypatch.setattr(manager_mod, "run_device_code_flow", lambda config, callbacks=None: captured)

    mgr, store = _manager(tmp_path, FakeClient(), grant_type=GrantType.DEVICE_CODE)
    tok = mgr.login()
    assert tok.access_token == "logged-in"
    # persisted + cached
    assert _borrow_token(store).access_token == "logged-in"
    assert _access(mgr) == "logged-in"


def test_login_dispatches_authorization_code(tmp_path, monkeypatch):
    import mote.runtime.models.auth.oauth.manager as manager_mod

    token = OAuthToken(access_token="ac-token", expires_at=time.time() + 3600)
    monkeypatch.setattr(manager_mod, "run_auth_code_flow", lambda config, callbacks=None: token)

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.AUTHORIZATION_CODE)
    assert mgr.login().access_token == "ac-token"


def test_login_rejects_headless_grant(tmp_path):
    from mote.runtime.models.auth.oauth.errors import OAuthConfigError

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.CLIENT_CREDENTIALS)
    with pytest.raises(OAuthConfigError):
        mgr.login()


def test_delete_commits_tombstone_and_clears_cache(tmp_path):
    manager, store = _manager(tmp_path, FakeClient())
    assert _access(manager) == "cc-1"
    before = store.load_metadata()
    assert before is not None

    receipt = manager.execute(
        CredentialCommand(
            command_id="revoke-test",
            subject=store.subject,
            action=CredentialAction.LOGOUT,
            authority_id="operator",
            expected_revision=before.revision,
            requested_at=datetime.now(timezone.utc),
        )
    )
    assert receipt.resulting_state is CredentialState.REVOKED

    after = store.load_metadata()
    assert after is not None
    assert after.revision == before.revision + 2  # REVOCATION_PENDING -> REVOKED
    assert after.secret_generation == before.secret_generation
    assert after.state.value == "revoked"
    assert after.secret_ref is None
    assert not hasattr(manager, "_cached")


def test_closed_maintenance_commands_preserve_evidence_and_erase_bearer(tmp_path):
    manager, _ = _manager(tmp_path, FakeClient())
    assert _access(manager) == "cc-1"
    store = manager._store
    current = store.load_metadata()
    assert current is not None

    hold = manager.execute(
        CredentialCommand(
            "hold",
            store.subject,
            CredentialAction.APPLY_HOLD,
            "legal-authority",
            current.revision,
            datetime.now(timezone.utc),
        )
    )
    assert hold.disposition is CredentialCommandDisposition.APPLIED
    held = store.load_metadata()
    assert held is not None and held.legal_hold

    rejected = manager.execute(
        CredentialCommand(
            "migrate",
            store.subject,
            CredentialAction.MIGRATE_BACKEND,
            "operator",
            held.revision,
            datetime.now(timezone.utc),
        )
    )
    assert rejected.disposition is CredentialCommandDisposition.REJECTED

    cleared = manager.execute(
        CredentialCommand(
            "clear",
            store.subject,
            CredentialAction.SECURITY_CLEAR,
            "security-authority",
            held.revision,
            datetime.now(timezone.utc),
        )
    )
    assert cleared.resulting_state is CredentialState.RETIRED
    terminal = store.load_metadata()
    assert terminal is not None and terminal.legal_hold and terminal.secret_ref is None
    assert not list((tmp_path / "vault").glob("*.secret"))


def test_interactive_grant_without_token_says_login_first(tmp_path):
    from mote.runtime.models.auth.oauth.errors import OAuthConfigError

    mgr, _ = _manager(tmp_path, FakeClient(), grant_type=GrantType.DEVICE_CODE)
    with pytest.raises(OAuthConfigError):
        _access(mgr)
