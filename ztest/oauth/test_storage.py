#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for credential storage: file round-trip + 0600 perms + fallback chain."""
from __future__ import annotations

import os
import stat

from mote.contracts.config.oauth import StoreBackend
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage import get_store
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


def test_file_corrupt_returns_none(tmp_path):
    store = FileCredentialStore("prov4", base_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json")
    assert store.load() is None


def test_file_mtime_observable(tmp_path):
    store = FileCredentialStore("prov5", base_dir=tmp_path)
    assert store.mtime() is None
    store.save(OAuthToken(access_token="abc"))
    assert isinstance(store.mtime(), float)


def test_get_store_file_backend(tmp_path):
    store = get_store("p", StoreBackend.FILE)
    assert isinstance(store, FileCredentialStore)


def test_fallback_degrades_to_file(tmp_path):
    # Simulate an unavailable keyring so the fallback uses the file store, and
    # point the file backend at tmp_path so the round-trip stays hermetic.
    store = FallbackCredentialStore("provfb")
    store._keyring = None
    store._file = FileCredentialStore("provfb", base_dir=tmp_path)

    tok = OAuthToken(access_token="zzz")
    store.save(tok)
    assert store.load().access_token == "zzz"
    store.delete()
    assert store.load() is None
