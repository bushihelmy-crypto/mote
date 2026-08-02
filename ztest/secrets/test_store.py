#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the encrypted two-section vault (``common/secrets/store.py``).

Covers the store's three tiers and their invariants: config auto-sync (hot on
mtime), persisted user secrets, in-memory session secrets; encrypted
persist/reload; section-isolated writes (a config reseed never clobbers user
secrets); and fail-closed reads for damaged protection state.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading

import pytest

from mote.runtime.secrets.cipher import AesGcmCipher
from mote.runtime.secrets.store import SecretStore

_API_KEY = "sk-proj-abc123SUPERsecretVALUE456"


def _cipher() -> AesGcmCipher:
    return AesGcmCipher(bytes(range(32)))


def _is_secret(path: str) -> bool:
    return "key" in path or "secret" in path or "token" in path or "password" in path


def _write_config(path, api_key: str) -> None:
    path.write_text(f"llm:\n  api_key: {api_key}\n  model: gpt-4o\nserver:\n  port: 8080\n")


def _bump_mtime(path) -> None:
    """Force a distinct mtime so refresh's mtime guard fires deterministically."""
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))


def _write_secret_process(vault_path: str, key: str, value: str, barrier) -> None:
    store = SecretStore(_cipher(), vault_path=vault_path)
    barrier.wait()
    store.add_user_secret(key, value)


class TestConfigHarvest:
    def test_construction_is_io_free(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        vault = tmp_path / "vault.json"
        _write_config(cfg, _API_KEY)

        SecretStore(_cipher(), vault_path=vault, config_path=cfg, is_secret=_is_secret)

        assert not vault.exists()

    def test_config_secrets_seeded_into_map(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", config_path=cfg, is_secret=_is_secret)
        assert store.as_map() == {_API_KEY: "<secret:llm.api_key>"}

    def test_nested_secret_leaf_detected(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("langfuse:\n  secret_key: lf-secretkey-abcdef123456\n")
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", config_path=cfg, is_secret=_is_secret)
        assert "<secret:langfuse.secret_key>" in store.as_map().values()

    def test_no_config_path_means_no_config_tier(self, tmp_path):
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault)
        assert store.as_map() == {}
        assert not vault.exists()


class TestUserAndSession:
    def test_named_secret_persists_and_reloads(self, tmp_path):
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault)
        label = store.add_user_secret("tg-token", "1234567890:AAnamedtokenvalue")
        assert label == "<agent-vault:tg-token>"
        # A fresh store over the same encrypted file recovers it.
        reloaded = SecretStore(_cipher(), vault_path=vault)
        assert reloaded.as_map() == {"1234567890:AAnamedtokenvalue": "<agent-vault:tg-token>"}

    def test_session_secret_is_memory_only(self, tmp_path):
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault)
        label = store.add_session_secret("anon-secret-value-123")
        assert label.startswith("<agent-vault:session-")
        assert "anon-secret-value-123" in store.as_map()
        # Not written to disk — a fresh store does not know it.
        reloaded = SecretStore(_cipher(), vault_path=vault)
        assert "anon-secret-value-123" not in reloaded.as_map()

    def test_vault_file_is_encrypted_on_disk(self, tmp_path):
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault)
        store.add_user_secret("k", "plaintext-should-not-appear-raw")
        raw = vault.read_bytes()
        assert b"plaintext-should-not-appear-raw" not in raw

    def test_vault_written_0600(self, tmp_path):
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault)
        store.add_user_secret("k", "some-secret-value")
        assert stat.S_IMODE(os.stat(vault).st_mode) == 0o600


class TestSectionIsolation:
    def test_config_reseed_preserves_user_secrets(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault, config_path=cfg, is_secret=_is_secret)
        store.add_user_secret("mine", "my-persisted-secret-value")

        # Edit config → reseed config section; user section must survive.
        _write_config(cfg, "sk-proj-DIFFERENTsecret999")
        _bump_mtime(cfg)
        m = store.as_map()
        assert "my-persisted-secret-value" in m  # user secret intact
        assert "sk-proj-DIFFERENTsecret999" in m  # new config value present
        assert _API_KEY not in m  # old config value gone

    def test_two_store_instances_merge_concurrent_named_secret_updates(self, tmp_path):
        vault = tmp_path / "vault.json"
        stores = (
            SecretStore(_cipher(), vault_path=vault),
            SecretStore(_cipher(), vault_path=vault),
        )
        barrier = threading.Barrier(2)

        def write(index: int) -> None:
            barrier.wait()
            stores[index].add_user_secret(f"key-{index}", f"secret-{index}")

        threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        restored = SecretStore(_cipher(), vault_path=vault)
        assert restored.get("key-0") == "secret-0"
        assert restored.get("key-1") == "secret-1"

    def test_two_processes_concurrently_create_and_merge_first_vault(self, tmp_path):
        vault = tmp_path / "vault.json"
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=_write_secret_process,
                args=(str(vault), f"process-{index}", f"value-{index}", barrier),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        restored = SecretStore(_cipher(), vault_path=vault)
        assert restored.get("process-0") == "value-0"
        assert restored.get("process-1") == "value-1"

    def test_config_reseed_and_named_upload_do_not_clobber_sections(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        vault = tmp_path / "vault.json"
        config_store = SecretStore(_cipher(), vault_path=vault, config_path=cfg, is_secret=_is_secret)
        config_store.prepare()
        upload_store = SecretStore(_cipher(), vault_path=vault)
        _write_config(cfg, "rotated-config-secret")
        _bump_mtime(cfg)
        barrier = threading.Barrier(2)

        def reseed() -> None:
            barrier.wait()
            config_store.refresh()

        def upload() -> None:
            barrier.wait()
            upload_store.add_user_secret("uploaded", "uploaded-secret")

        threads = [threading.Thread(target=reseed), threading.Thread(target=upload)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        restored = SecretStore(_cipher(), vault_path=vault)
        values = restored.as_map()
        assert "rotated-config-secret" in values
        assert "uploaded-secret" in values


class TestHotReload:
    def test_config_edit_reseeds_without_restart(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", config_path=cfg, is_secret=_is_secret)
        assert _API_KEY in store.as_map()

        new_key = "sk-proj-rotatedKEY-0987654321"
        _write_config(cfg, new_key)
        _bump_mtime(cfg)
        m = store.as_map()
        assert new_key in m
        assert _API_KEY not in m

    def test_external_vault_write_is_reloaded(self, tmp_path):
        vault = tmp_path / "vault.json"
        store_a = SecretStore(_cipher(), vault_path=vault)
        store_b = SecretStore(_cipher(), vault_path=vault)
        # store_a writes a named secret; store_b sees it on next refresh (mtime).
        store_a.add_user_secret("shared", "shared-secret-value-xyz")
        _bump_mtime(vault)
        assert "shared-secret-value-xyz" in store_b.as_map()


class TestSecretsConfigFile:
    """The plaintext, human-edited ``secrets_config.json`` -> file section."""

    def _write_sc(self, path, mapping: dict) -> None:
        path.write_text(json.dumps(mapping))

    def test_file_secret_seeded_and_labeled(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"tg-token": "1234567890:AAfilesecretvalue"})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        assert store.as_map() == {"1234567890:AAfilesecretvalue": "<agent-vault:tg-token>"}

    def test_file_section_encrypted_on_disk(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"k": "plaintext-file-secret-xyz"})
        vault = tmp_path / "vault.json"
        store = SecretStore(_cipher(), vault_path=vault, secrets_config_file=sc)
        store.prepare()
        # The value is vaulted encrypted, not stored raw in the vault blob.
        assert b"plaintext-file-secret-xyz" not in vault.read_bytes()

    def test_hot_add_new_entry(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"a": "aaaa-secret-value"})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        assert "aaaa-secret-value" in store.as_map()

        self._write_sc(sc, {"a": "aaaa-secret-value", "b": "bbbb-secret-value"})
        _bump_mtime(sc)
        m = store.as_map()
        assert "aaaa-secret-value" in m
        assert "bbbb-secret-value" in m

    def test_hot_delete_single_entry(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"a": "aaaa-secret-value", "b": "bbbb-secret-value"})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        assert "bbbb-secret-value" in store.as_map()

        # Remove one name from the file -> dropped from the vault (full replace).
        self._write_sc(sc, {"a": "aaaa-secret-value"})
        _bump_mtime(sc)
        m = store.as_map()
        assert "aaaa-secret-value" in m
        assert "bbbb-secret-value" not in m

    def test_hot_delete_whole_file(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"a": "aaaa-secret-value"})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        assert "aaaa-secret-value" in store.as_map()

        sc.unlink()  # delete the entire file -> section cleared
        assert "aaaa-secret-value" not in store.as_map()

    def test_deleted_file_cleared_on_restart(self, tmp_path):
        """A stale entry from a since-deleted file is cleared on construct."""
        sc = tmp_path / "secrets_config.json"
        vault = tmp_path / "vault.json"
        self._write_sc(sc, {"a": "aaaa-secret-value"})
        SecretStore(_cipher(), vault_path=vault, secrets_config_file=sc)
        sc.unlink()
        # Fresh store: file gone, but the vault still holds the old encrypted entry.
        # The _UNSET-sentinel first reconcile must clear it.
        reloaded = SecretStore(_cipher(), vault_path=vault, secrets_config_file=sc)
        assert "aaaa-secret-value" not in reloaded.as_map()

    def test_file_isolated_from_config_and_user(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"file-key": "file-secret-value-123"})
        vault = tmp_path / "vault.json"
        store = SecretStore(
            _cipher(),
            vault_path=vault,
            config_path=cfg,
            secrets_config_file=sc,
            is_secret=_is_secret,
        )
        store.add_user_secret("user-key", "user-secret-value-456")

        m = store.as_map()
        assert m[_API_KEY] == "<secret:llm.api_key>"  # config tier
        assert m["file-secret-value-123"] == "<agent-vault:file-key>"  # file tier
        assert m["user-secret-value-456"] == "<agent-vault:user-key>"  # user tier

    def test_malformed_file_fails_closed(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        sc.write_text("{not valid json")
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        with pytest.raises(ValueError, match="malformed"):
            store.as_map()

    def test_non_string_values_fail_closed(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        sc.write_text(json.dumps({"good": "keepme-secret-value", "bad": 12345}))
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        with pytest.raises(ValueError, match="strings"):
            store.as_map()


class TestLabels:
    """``labels()`` — the {name: placeholder} discovery map (names only, no values)."""

    def _write_sc(self, path, mapping: dict) -> None:
        path.write_text(json.dumps(mapping))

    def test_empty_when_nothing_configured(self, tmp_path):
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        assert store.labels() == {}

    def test_three_tier_merge_with_prefixes(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_config(cfg, _API_KEY)
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"xhs_phone": "13800000000"})
        store = SecretStore(
            _cipher(),
            vault_path=tmp_path / "vault.json",
            config_path=cfg,
            secrets_config_file=sc,
            is_secret=_is_secret,
        )
        store.add_user_secret("gh_token", "ghp_uservalue123")
        labels = store.labels()
        # config tier keyed by dotted path
        assert labels["llm.api_key"] == "<secret:llm.api_key>"
        # file tier keyed by name
        assert labels["xhs_phone"] == "<agent-vault:xhs_phone>"
        # user tier keyed by name
        assert labels["gh_token"] == "<agent-vault:gh_token>"

    def test_excludes_session_tier(self, tmp_path):
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        store.add_session_secret("anon-value-xyz")
        # Session keys are random session-<uuid> names → never surfaced for discovery.
        assert store.labels() == {}

    def test_labels_never_leak_values(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"pw": "super-secret-plaintext-value"})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        labels = store.labels()
        assert "pw" in labels
        # Only the placeholder, never the plaintext.
        assert "super-secret-plaintext-value" not in labels
        assert "super-secret-plaintext-value" not in "".join(labels.values())

    def test_empty_valued_entries_skipped(self, tmp_path):
        sc = tmp_path / "secrets_config.json"
        self._write_sc(sc, {"has_value": "vvvv-secret", "blank": ""})
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", secrets_config_file=sc)
        labels = store.labels()
        assert "has_value" in labels
        assert "blank" not in labels


class TestFailClosed:
    def test_undecryptable_vault_raises(self, tmp_path):
        vault = tmp_path / "vault.json"
        # A vault encrypted with one key, opened with another → empty (no crash).
        SecretStore(AesGcmCipher(bytes(32)), vault_path=vault).add_user_secret("k", "v-secret-value")
        other = SecretStore(AesGcmCipher(b"\x02" * 32), vault_path=vault)
        with pytest.raises(ValueError, match="undecryptable"):
            other.as_map()

    def test_garbage_vault_raises(self, tmp_path):
        vault = tmp_path / "vault.json"
        vault.write_bytes(b"not an encrypted blob")
        store = SecretStore(_cipher(), vault_path=vault)
        with pytest.raises(ValueError, match="undecryptable"):
            store.as_map()

    def test_malformed_config_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("this: : : not: valid: yaml: [")
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json", config_path=cfg, is_secret=_is_secret)
        with pytest.raises(ValueError, match="malformed"):
            store.as_map()

    @pytest.mark.parametrize(
        "payload",
        (
            {
                "schema": "mote.secret-vault/v2",
                "schema_version": 2,
                "revision": 1,
                "sections": {"config": {}, "file": {}, "secrets": {}},
            },
            {
                "schema": "mote.secret-vault/v1",
                "schema_version": 1,
                "revision": 1,
                "sections": {"config": {}, "file": {}, "secrets": {}, "future": {}},
            },
            {
                "schema": "mote.secret-vault/v1",
                "schema_version": 1,
                "revision": 1,
                "sections": {"config": {"bad": 1}, "file": {}, "secrets": {}},
            },
        ),
    )
    def test_unknown_schema_extra_section_and_non_string_value_fail_closed(self, tmp_path, payload):
        vault = tmp_path / "vault.json"
        vault.write_bytes(_cipher().encrypt(json.dumps(payload).encode("utf-8")))
        with pytest.raises(ValueError):
            SecretStore(_cipher(), vault_path=vault).prepare()

    def test_noncanonical_vault_fails_closed(self, tmp_path):
        vault = tmp_path / "vault.json"
        noncanonical = {"config": {}, "file": {}, "secrets": {"old": "secret"}}
        vault.write_bytes(_cipher().encrypt(json.dumps(noncanonical).encode("utf-8")))
        store = SecretStore(_cipher(), vault_path=vault)
        with pytest.raises(ValueError, match="envelope"):
            store.prepare()
