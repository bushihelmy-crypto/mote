"""Architecture gate for the R2.33 encrypted vault transaction owner."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_secret_vault_has_one_strict_versioned_envelope() -> None:
    source = (ROOT / "runtime/secrets/store.py").read_text(encoding="utf-8")
    assert '"mote.secret-vault/v1"' in source
    assert "def _decode_vault" in source
    assert "set(raw)" in source
    assert 'type(raw["schema_version"]) is not int' in source
    assert "def _string_map(" not in source
    assert "migrate_legacy_vault" not in source


def test_all_vault_mutations_share_lock_and_canonical_durable_writer() -> None:
    store = (ROOT / "runtime/secrets/store.py").read_text(encoding="utf-8")
    atomic = (ROOT / "runtime/persistence/atomic.py").read_text(encoding="utf-8")
    assert "FileLock(str(self._vault_lock_path))" in store
    assert "current = self._read_vault()" in store
    assert "atomic_write(self._vault_path, blob, fsync=True, mode=0o600)" in store
    assert 'with_name(self._vault_path.name + ".tmp")' not in store
    assert "tempfile.mkstemp" in atomic
    assert "os.fsync" in atomic and "os.replace" in atomic


def test_vault_key_corruption_is_fail_closed_and_generation_is_durable() -> None:
    cipher = (ROOT / "runtime/secrets/cipher.py").read_text(encoding="utf-8")
    assert "vault key file has an invalid length" in cipher
    assert "vault key file permissions are not owner-only" in cipher
    assert "atomic_write(self._path, key, fsync=True, mode=0o600)" in cipher
    assert "fail-open" not in cipher
