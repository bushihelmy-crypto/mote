#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BrowserProfileStore — encrypted durable browser-login store.

Verifies the round-trip (encrypt → disk → decrypt), that the bytes on disk are
genuinely ciphertext (not plaintext cookies), the best-effort miss/failure
paths (missing / corrupt / wrong-key → ``None``, never raise), name-slug path
safety, owner-only file mode, and lazy cipher construction.
"""
from __future__ import annotations

import json
import os
import stat

from mote.common.secrets.cipher import AesGcmCipher
from mote.executor.dependency.browser_profile import BrowserProfileStore

_STATE = {"cookies": [{"name": "sid", "value": "secret-token", "domain": ".x.com"}], "origins": []}


def _store(tmp_path, *, key: bytes = b"\x01" * 32):
    """A store rooted at *tmp_path* using a fixed AES key (no vault.key on disk)."""
    return BrowserProfileStore(lambda: AesGcmCipher(key), root=tmp_path)


class TestRoundTrip:
    def test_save_then_load(self, tmp_path):
        store = _store(tmp_path)
        store.save("xhs", _STATE)
        assert store.load("xhs") == _STATE

    def test_on_disk_is_ciphertext_not_plaintext(self, tmp_path):
        store = _store(tmp_path)
        store.save("xhs", _STATE)
        raw = store.path_for("xhs").read_bytes()
        # The session token must NOT appear in the stored bytes.
        assert b"secret-token" not in raw
        # ...and it must not be readable JSON either.
        try:
            json.loads(raw.decode("utf-8"))
            decoded_json = True
        except Exception:  # noqa: BLE001
            decoded_json = False
        assert decoded_json is False

    def test_file_is_owner_only_0600(self, tmp_path):
        store = _store(tmp_path)
        store.save("xhs", _STATE)
        mode = stat.S_IMODE(os.stat(store.path_for("xhs")).st_mode)
        assert mode == 0o600

    def test_overwrite_replaces(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", {"cookies": [1]})
        store.save("p", {"cookies": [2]})
        assert store.load("p") == {"cookies": [2]}


class TestMissAndFailure:
    def test_missing_profile_returns_none(self, tmp_path):
        assert _store(tmp_path).load("never-saved") is None

    def test_empty_name_returns_none(self, tmp_path):
        assert _store(tmp_path).load("") is None

    def test_corrupt_file_returns_none(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", _STATE)
        store.path_for("p").write_bytes(b"not a valid token")
        assert store.load("p") is None

    def test_wrong_key_returns_none(self, tmp_path):
        # Written with one key, read with another → auth failure → None (no raise).
        _store(tmp_path, key=b"\x01" * 32).save("p", _STATE)
        assert _store(tmp_path, key=b"\x02" * 32).load("p") is None

    def test_save_none_is_noop(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", None)
        assert not store.path_for("p").exists()

    def test_save_empty_is_noop(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", {})
        assert not store.path_for("p").exists()

    def test_save_empty_does_not_clobber_existing(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", _STATE)
        store.save("p", None)  # ignored — existing login must survive
        assert store.load("p") == _STATE


class TestForget:
    def test_forget_removes(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", _STATE)
        store.forget("p")
        assert store.load("p") is None
        assert not store.path_for("p").exists()

    def test_forget_missing_is_noop(self, tmp_path):
        _store(tmp_path).forget("never")  # must not raise


class TestNameSafety:
    def test_slug_confines_to_root(self, tmp_path):
        store = _store(tmp_path)
        # A traversal-looking name must not escape the profile directory.
        path = store.path_for("../../etc/passwd")
        assert tmp_path in path.parents
        assert path.suffix == ".profile"

    def test_traversal_name_round_trips_within_root(self, tmp_path):
        store = _store(tmp_path)
        store.save("../evil", _STATE)
        # Saved and loadable, but the file lives under the root (slug-sanitized).
        assert store.load("../evil") == _STATE
        assert tmp_path in store.path_for("../evil").parents

    def test_distinct_names_distinct_files(self, tmp_path):
        store = _store(tmp_path)
        assert store.path_for("a") != store.path_for("b")


class TestLazyCipher:
    def test_cipher_not_built_until_used(self, tmp_path):
        built = []

        def factory():
            built.append(1)
            return AesGcmCipher(b"\x03" * 32)

        store = BrowserProfileStore(factory, root=tmp_path)
        assert built == []  # construction alone builds nothing
        store.save("p", _STATE)
        store.load("p")
        assert built == [1]  # built once, then cached
