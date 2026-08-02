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

import pytest

from mote.contracts.browser import BrowserProfileConflictError, BrowserProfileError, BrowserProfileNotFoundError
from mote.runtime.interactive.browser.profile import BrowserProfileStore, decode_storage_state
from mote.runtime.secrets.cipher import AesGcmCipher

_STATE = {
    "cookies": [
        {
            "name": "sid",
            "value": "secret-token",
            "domain": ".x.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}
_TYPED_STATE = decode_storage_state(_STATE)


def _store(tmp_path, *, key: bytes = b"\x01" * 32):
    """A store rooted at *tmp_path* using a fixed AES key (no vault.key on disk)."""
    return BrowserProfileStore(lambda: AesGcmCipher(key), root=tmp_path)


class TestRoundTrip:
    def test_save_then_load(self, tmp_path):
        store = _store(tmp_path)
        store.save("xhs", _TYPED_STATE, expected_revision=None)
        assert store.load("xhs").storage_state.to_payload() == _STATE

    def test_on_disk_is_ciphertext_not_plaintext(self, tmp_path):
        store = _store(tmp_path)
        store.save("xhs", _TYPED_STATE, expected_revision=None)
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
        store.save("xhs", _TYPED_STATE, expected_revision=None)
        mode = stat.S_IMODE(os.stat(store.path_for("xhs")).st_mode)
        assert mode == 0o600

    def test_overwrite_replaces(self, tmp_path):
        store = _store(tmp_path)
        first = store.save("p", _TYPED_STATE, expected_revision=None)
        second = store.save("p", _TYPED_STATE, expected_revision=first.revision)
        assert second.revision == 2


class TestMissAndFailure:
    def test_missing_profile_returns_none(self, tmp_path):
        with pytest.raises(BrowserProfileNotFoundError):
            _store(tmp_path).load("never-saved")

    def test_empty_name_returns_none(self, tmp_path):
        with pytest.raises(BrowserProfileError):
            _store(tmp_path).load("")

    def test_corrupt_file_returns_none(self, tmp_path):
        store = _store(tmp_path)
        store.save("p", _TYPED_STATE, expected_revision=None)
        store.path_for("p").write_bytes(b"not a valid token")
        with pytest.raises(BrowserProfileError):
            store.load("p")

    def test_wrong_key_returns_none(self, tmp_path):
        # Written with one key, read with another → auth failure → None (no raise).
        _store(tmp_path, key=b"\x01" * 32).save("p", _TYPED_STATE, expected_revision=None)
        with pytest.raises(BrowserProfileError):
            _store(tmp_path, key=b"\x02" * 32).load("p")

    def test_save_none_is_noop(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(BrowserProfileError):
            store.save("p", None, expected_revision=None)
        assert not store.path_for("p").exists()

    def test_save_empty_is_noop(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(BrowserProfileError):
            store.save("p", {}, expected_revision=None)
        assert not store.path_for("p").exists()

    def test_save_empty_does_not_clobber_existing(self, tmp_path):
        store = _store(tmp_path)
        receipt = store.save("p", _TYPED_STATE, expected_revision=None)
        with pytest.raises(BrowserProfileError):
            store.save("p", None, expected_revision=receipt.revision)
        assert store.load("p").revision == receipt.revision


class TestForget:
    def test_forget_removes(self, tmp_path):
        store = _store(tmp_path)
        receipt = store.save("p", _TYPED_STATE, expected_revision=None)
        store.forget("p", expected_revision=receipt.revision)
        with pytest.raises(BrowserProfileNotFoundError):
            store.load("p")
        assert not store.path_for("p").exists()

    def test_forget_missing_is_noop(self, tmp_path):
        with pytest.raises(BrowserProfileNotFoundError):
            _store(tmp_path).forget("never", expected_revision=1)


class TestNameSafety:
    def test_slug_confines_to_root(self, tmp_path):
        store = _store(tmp_path)
        # A traversal-looking name must not escape the profile directory.
        with pytest.raises(BrowserProfileError):
            store.path_for("../../etc/passwd")

    def test_traversal_name_round_trips_within_root(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(BrowserProfileError):
            store.save("../evil", _TYPED_STATE, expected_revision=None)

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
        store.save("p", _TYPED_STATE, expected_revision=None)
        store.load("p")
        assert built == [1]  # built once, then cached


def test_two_store_instances_use_revision_cas(tmp_path):
    first_store = _store(tmp_path)
    second_store = _store(tmp_path)
    first = first_store.save("account", _TYPED_STATE, expected_revision=None)
    second = second_store.save("account", _TYPED_STATE, expected_revision=first.revision)
    with pytest.raises(BrowserProfileConflictError):
        first_store.save("account", _TYPED_STATE, expected_revision=first.revision)
    assert second_store.load("account").revision == second.revision


@pytest.mark.parametrize(
    "state",
    [
        {"cookies": [], "origins": [], "extra": True},
        {"cookies": [{"name": "sid"}], "origins": []},
        {"cookies": [], "origins": [{"origin": "x", "localStorage": [{"name": "x", "value": 1}]}]},
    ],
)
def test_storage_state_decoder_fails_closed(state):
    with pytest.raises(BrowserProfileError):
        decode_storage_state(state)
