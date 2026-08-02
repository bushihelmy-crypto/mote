#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for credential storage: file round-trip + 0600 perms + fallback chain."""

from __future__ import annotations

import json
import os
import stat

import pytest

from mote.contracts.config.model.oauth import StoreBackend
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage import get_store
from mote.runtime.models.auth.oauth.storage.base import credential_subject
from mote.runtime.models.auth.oauth.storage.fallback_store import FallbackCredentialStore
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore


def test_file_round_trip(tmp_path):
    store = FileCredentialStore("prov1", base_dir=tmp_path)
    assert store.load() is None  # nothing stored yet

    tok = OAuthToken(access_token="abc", refresh_token="r1", expires_at=123.0, scopes=["a", "b"])
    store.save(tok)

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "abc"
    assert loaded.refresh_token == "r1"
    assert loaded.scopes == ["a", "b"]


def test_file_perms_are_0600(tmp_path):
    store = FileCredentialStore("prov2", base_dir=tmp_path)
    store.save(OAuthToken(access_token="abc"))
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_file_delete_idempotent(tmp_path):
    store = FileCredentialStore("prov3", base_dir=tmp_path)
    store.delete()  # no error when absent
    store.save(OAuthToken(access_token="abc"))
    assert store.load() is not None
    store.delete()
    assert store.load() is None


def test_file_corrupt_fails_closed(tmp_path):
    store = FileCredentialStore("prov4", base_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json")
    store.path.chmod(0o600)
    with pytest.raises(ValueError, match="corrupt OAuth credential"):
        store.load()


def test_get_store_file_backend(tmp_path):
    store = get_store("p", StoreBackend.FILE, base_dir=tmp_path)
    assert isinstance(store, FileCredentialStore)


def test_fallback_selects_file_once_when_keyring_is_unavailable(tmp_path, monkeypatch):
    # Simulate an unavailable keyring so the fallback uses the file store, and
    # point the file backend at tmp_path so the round-trip stays hermetic.
    import mote.runtime.models.auth.oauth.storage.fallback_store as fallback

    class UnavailableKeyring:
        def __init__(self, provider):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(fallback, "KeyringCredentialStore", UnavailableKeyring)
    store = FallbackCredentialStore("provfb", tmp_path)

    tok = OAuthToken(access_token="zzz")
    store.save(tok)
    assert store.load().access_token == "zzz"
    store.delete()
    assert store.load() is None


@pytest.mark.parametrize("name", ["../escape", "/tmp/absolute", "a/b", "a\\b"])
def test_external_name_never_becomes_a_path_segment(tmp_path, name):
    store = FileCredentialStore(name, base_dir=tmp_path)
    store.save(OAuthToken(access_token="safe"))

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
        store.load()


def test_boolean_credential_version_fails_closed(tmp_path):
    store = FileCredentialStore("boolean-version", base_dir=tmp_path)
    store.save(OAuthToken(access_token="secret"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["version"] = True
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    store.path.chmod(0o600)

    with pytest.raises(ValueError, match="version"):
        store.load()


def test_boolean_backend_selection_version_fails_closed(tmp_path):
    subject = credential_subject("boolean-selection")
    selection = tmp_path / f"{subject}.backend.json"
    selection.write_text(
        json.dumps({"version": True, "subject": subject, "backend": "file"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="backend selection"):
        FallbackCredentialStore("boolean-selection", tmp_path)


def test_symlink_record_fails_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    store = FileCredentialStore("symlink", base_dir=tmp_path / "oauth")
    store.path.parent.mkdir(parents=True)
    store.path.symlink_to(outside)

    with pytest.raises(PermissionError):
        store.load()
