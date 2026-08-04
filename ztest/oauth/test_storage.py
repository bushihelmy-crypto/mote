#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the explicitly selected OAuth credential backend."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.config.model.oauth import StoreBackend
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage import get_store
from mote.runtime.models.auth.oauth.storage import keyring_store as keyring_backend
from mote.runtime.models.auth.oauth.storage.base import CredentialUse
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore


def _use(store: FileCredentialStore) -> CredentialUse:
    return CredentialUse(store.external_name, "test-account", (), "test-consumer")


def _publish(store: FileCredentialStore, token: OAuthToken) -> None:
    current = store.load_metadata()
    store.publish(token, expected_revision=0 if current is None else current.revision)


def _borrow(store: FileCredentialStore):
    return store.borrow(_use(store), expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))


def test_file_round_trip(tmp_path):
    store = FileCredentialStore("prov1", base_dir=tmp_path)
    assert _borrow(store) is None

    tok = OAuthToken(access_token="abc", refresh_token="r1", expires_at=123.0, scopes=["a", "b"])
    _publish(store, tok)

    borrowed = _borrow(store)
    assert borrowed is not None
    loaded = borrowed.token
    assert loaded is not None
    assert loaded.access_token == "abc"
    assert loaded.refresh_token == "r1"
    assert loaded.scopes == ["a", "b"]
    store.release_borrow(borrowed)


def test_borrow_is_generation_bound_released_and_revoked_on_publish(tmp_path):
    store = FileCredentialStore("borrowed", base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="first"))
    first = _borrow(store)
    assert first is not None
    marker = tmp_path / "borrows" / str(store.subject) / f"{first.borrow_id}.json"
    assert marker.is_file()

    _publish(store, OAuthToken(access_token="second"))

    assert not marker.exists()
    store.release_borrow(first)
    second = _borrow(store)
    assert second is not None
    assert second.metadata.secret_generation == first.metadata.secret_generation + 1
    assert second.token.access_token == "second"
    store.release_borrow(second)


def test_borrow_expiry_is_bounded_and_capacity_is_not_silent(tmp_path):
    store = FileCredentialStore("bounded", base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="secret"))
    with pytest.raises(ValueError, match="hard operation bound"):
        store.borrow(_use(store), expires_at=datetime.now(timezone.utc) + timedelta(minutes=31))

    borrows = [_borrow(store) for _ in range(64)]
    assert all(borrow is not None for borrow in borrows)
    with pytest.raises(RuntimeError, match="capacity"):
        _borrow(store)
    for borrow in borrows:
        assert borrow is not None
        store.release_borrow(borrow)


def test_keyring_is_secret_vault_only_with_fenced_file_metadata(tmp_path, monkeypatch):
    class _Keyring:
        def __init__(self) -> None:
            self.values = {}

        def get_password(self, service, key):
            return self.values.get((service, key))

        def set_password(self, service, key, value):
            self.values[(service, key)] = value

        def delete_password(self, service, key):
            self.values.pop((service, key), None)

    vault = _Keyring()
    monkeypatch.setattr(keyring_backend, "keyring", vault)
    store = keyring_backend.KeyringCredentialStore("keyring-provider", tmp_path)
    first = store.publish(OAuthToken(access_token="first"), expected_revision=0)
    assert store.path.is_file()
    assert json.loads(store.path.read_bytes())["backend"] == "keyring"

    borrowed = store.borrow(
        CredentialUse("keyring-provider", "account", (), "consumer"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert borrowed is not None and borrowed.token.access_token == "first"
    second = store.publish(OAuthToken(access_token="second"), expected_revision=first.revision)

    assert second.secret_generation == 2
    assert not (tmp_path / "borrows" / str(store.subject) / f"{borrowed.borrow_id}.json").exists()
    assert all(not key.endswith(":1") for _, key in vault.values.keys())


def test_file_perms_are_0600(tmp_path):
    store = FileCredentialStore("prov2", base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="abc"))
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_file_delete_idempotent(tmp_path):
    store = FileCredentialStore("prov3", base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="abc"))
    assert _borrow(store) is not None
    metadata = store.load_metadata()
    assert metadata is not None
    from mote.runtime.models.auth.oauth.storage.base import CredentialState

    store.transition(CredentialState.REVOKED, expected_revision=metadata.revision)
    assert _borrow(store) is None


def test_file_corrupt_fails_closed(tmp_path):
    store = FileCredentialStore("prov4", base_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json")
    store.path.chmod(0o600)
    with pytest.raises(ValueError, match="corrupt OAuth credential"):
        store.load_metadata()


def test_get_store_file_backend(tmp_path):
    store = get_store("p", StoreBackend.FILE, base_dir=tmp_path)
    assert isinstance(store, FileCredentialStore)


@pytest.mark.parametrize("name", ["../escape", "/tmp/absolute", "a/b", "a\\b"])
def test_external_name_never_becomes_a_path_segment(tmp_path, name):
    store = FileCredentialStore(name, base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="safe"))

    assert store.path.parent == tmp_path.resolve()
    assert store.path.name.startswith("oauth_")
    assert name not in store.path.name


def test_subject_names_do_not_slug_collide(tmp_path):
    first = FileCredentialStore("a/b", base_dir=tmp_path)
    second = FileCredentialStore("a\\b", base_dir=tmp_path)

    assert first.subject != second.subject
    assert first.path != second.path


def test_unknown_record_shape_fails_closed(tmp_path):
    store = FileCredentialStore("strict", base_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    store.path.chmod(0o600)

    with pytest.raises(ValueError):
        store.load_metadata()


def test_boolean_credential_version_fails_closed(tmp_path):
    store = FileCredentialStore("boolean-version", base_dir=tmp_path)
    _publish(store, OAuthToken(access_token="secret"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["version"] = True
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    store.path.chmod(0o600)

    with pytest.raises(ValueError, match="strict v2"):
        store.load_metadata()


def test_symlink_record_fails_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    store = FileCredentialStore("symlink", base_dir=tmp_path / "oauth")
    store.path.parent.mkdir(parents=True)
    store.path.symlink_to(outside)

    with pytest.raises(PermissionError):
        store.load_metadata()
