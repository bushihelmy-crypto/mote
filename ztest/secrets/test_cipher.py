#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the vault cipher strategy (``common/secrets/cipher.py``).

Pins the crypto floor the encrypted vault stands on: AES-256-GCM round-trips,
authenticates (a wrong key fails loud rather than returning garbage), and the
auto-generated key file is owner-only (0600). ``build_cipher`` is the swappable
strategy registry the store depends on.
"""

from __future__ import annotations

import os
import stat

import pytest

from mote.product.config.secrets import SecretsConfig
from mote.runtime.secrets.cipher import AesGcmCipher, KeyFileProvider, build_cipher


class TestAesGcmCipher:
    def test_roundtrip(self):
        cipher = AesGcmCipher(bytes(range(32)))
        token = cipher.encrypt(b"top secret bytes")
        assert cipher.decrypt(token) == b"top secret bytes"

    def test_nonce_makes_ciphertext_nondeterministic(self):
        cipher = AesGcmCipher(bytes(range(32)))
        assert cipher.encrypt(b"same") != cipher.encrypt(b"same")

    def test_wrong_key_fails_loud(self):
        token = AesGcmCipher(bytes(32)).encrypt(b"payload")
        with pytest.raises(Exception):
            AesGcmCipher(b"\x01" * 32).decrypt(token)

    def test_short_token_rejected(self):
        with pytest.raises(ValueError):
            AesGcmCipher(bytes(32)).decrypt(b"short")

    def test_bad_key_length_rejected(self):
        with pytest.raises(ValueError):
            AesGcmCipher(b"tooshort")


class TestKeyFileProvider:
    def test_generates_32_byte_key_at_0600(self, tmp_path):
        key_file = tmp_path / "vault.key"
        provider = KeyFileProvider(key_file)
        key = provider.key()
        assert len(key) == 32
        mode = stat.S_IMODE(os.stat(key_file).st_mode)
        assert mode == 0o600

    def test_key_is_stable_across_reads(self, tmp_path):
        provider = KeyFileProvider(tmp_path / "vault.key")
        assert provider.key() == provider.key()

    def test_wrong_length_key_fails_closed_without_destroying_evidence(self, tmp_path):
        key_file = tmp_path / "vault.key"
        evidence = b"corrupt"
        key_file.write_bytes(evidence)
        with pytest.raises(ValueError, match="invalid length"):
            KeyFileProvider(key_file).key()
        assert key_file.read_bytes() == evidence


class TestBuildCipher:
    def test_aes_strategy(self, tmp_path):
        # build_cipher reads ``key_path`` via getattr — a SimpleNamespace stands
        # in so the test's key lands in tmp, not the real ~/.mote/vault.key.
        from types import SimpleNamespace

        cfg = SimpleNamespace(cipher="aes", key_path=str(tmp_path / "vault.key"))
        cipher = build_cipher(cfg)
        assert not (tmp_path / "vault.key").exists()
        assert cipher.decrypt(cipher.encrypt(b"x")) == b"x"

    def test_unknown_strategy_fails_loud(self):
        with pytest.raises(ValueError, match="unknown vault cipher"):
            build_cipher(SecretsConfig(cipher="rot13"))
