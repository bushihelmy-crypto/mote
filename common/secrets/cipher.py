"""Vault encryption — the pluggable key/cipher strategy behind the secret vault.

The vault file (``~/.mote/secrets.json``) is stored **encrypted**, never as
plaintext JSON: a leaked file, a stray backup, or a ``cat`` of the config dir
does not expose the raw secret values. The cipher is deliberately small and
swappable so the *key source* is a policy choice, not a hard-coded assumption:

* :class:`VaultCipher` — the one-method-pair contract (``encrypt``/``decrypt`` on
  ``bytes``) the store depends on. The store never learns which algorithm or key
  source is behind it.
* :class:`AesGcmCipher` — the first (and only shipped) algorithm: AES-256-GCM
  (authenticated: a wrong key / tampered ciphertext fails ``decrypt`` loudly, it
  does not silently return garbage). A fresh 96-bit nonce is prepended to every
  ciphertext, so the same plaintext encrypts differently each write.
* :class:`KeyFileProvider` — the first key source: a 32-byte key file at
  ``~/.mote/vault.key``, auto-generated on first use and locked to ``0600``.
  keyring is intentionally *not* used — no usable backend exists on the target
  WSL2 host (``NoKeyringError``), so a self-managed key file is the honest floor.
* :func:`build_cipher` — a tiny name→builder registry (``"aes"`` today), so a new
  strategy (e.g. a passphrase-derived scrypt key via ``MOTE_VAULT_PASSPHRASE``)
  slots in as one more entry without touching the store or the subscribers.

Leaf module: imports only stdlib + ``cryptography``. It has zero knowledge of the
event bus, the config loader, or the store — the store depends on it, never the
reverse.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mote.common.const.paths import CONFIG_ROOT

#: AES-256 key length in bytes.
_KEY_BYTES = 32
#: GCM nonce length in bytes (96 bits — the AES-GCM standard nonce size).
_NONCE_BYTES = 12
#: The default key-file location (sibling of the vault, in the config dir).
_KEY_FILE = "vault.key"


@runtime_checkable
class VaultCipher(Protocol):
    """Round-trip byte encryption — the only crypto surface the store depends on."""

    def encrypt(self, data: bytes) -> bytes:
        """Return an opaque ciphertext token for ``data`` (nonce embedded)."""
        ...

    def decrypt(self, token: bytes) -> bytes:
        """Recover the plaintext from a token produced by :meth:`encrypt`.

        Raises on a wrong key or a tampered/short token — the store treats any
        such failure as an empty vault (fail-open), so a corrupt file never
        bricks a turn; it just means fewer known secrets.
        """
        ...


class AesGcmCipher:
    """AES-256-GCM with a random per-message nonce prepended to the ciphertext."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise ValueError(f"AES-256 key must be {_KEY_BYTES} bytes, got {len(key)}")
        self._aead = AESGCM(key)

    def encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt(self, token: bytes) -> bytes:
        if len(token) <= _NONCE_BYTES:
            raise ValueError("ciphertext token too short to contain a nonce")
        nonce, ciphertext = token[:_NONCE_BYTES], token[_NONCE_BYTES:]
        return self._aead.decrypt(nonce, ciphertext, None)


class KeyFileProvider:
    """A 32-byte key persisted at ``~/.mote/vault.key``, auto-generated + 0600."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else CONFIG_ROOT / _KEY_FILE

    @property
    def path(self) -> Path:
        return self._path

    def key(self) -> bytes:
        """Return the key bytes, generating and locking the file on first use.

        An existing file of the right length is read as-is; a missing (or
        wrong-length) file is (re)generated with ``os.urandom`` and chmod-ed to
        ``0600`` so the secret-decrypting key is owner-only.
        """
        try:
            existing = self._path.read_bytes()
            if len(existing) == _KEY_BYTES:
                return existing
        except OSError:
            pass
        return self._generate()

    def _generate(self) -> bytes:
        key = os.urandom(_KEY_BYTES)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create restricted from the start: open with 0600 rather than write-then-chmod
        # so the key is never briefly world-readable on disk.
        fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        os.chmod(self._path, 0o600)
        return key


def build_cipher(config) -> VaultCipher:
    """Resolve a :class:`VaultCipher` from a ``SecretsConfig``-shaped object.

    Reads ``config.cipher`` (the strategy name) and dispatches through the
    registry. Unknown names fail loud — a typo in a security knob must not
    silently fall back to a weaker (or no) cipher.
    """
    name = getattr(config, "cipher", "aes") or "aes"
    builder = _REGISTRY.get(name)
    if builder is None:
        raise ValueError(f"unknown vault cipher strategy {name!r}; known: {sorted(_REGISTRY)}")
    return builder(config)


def _build_aes(config) -> VaultCipher:
    key_path = getattr(config, "key_path", None)
    provider = KeyFileProvider(Path(key_path) if key_path else None)
    return AesGcmCipher(provider.key())


#: name → cipher builder. Add a strategy here (one line) to make it selectable
#: via ``config.secrets.cipher`` without touching the store or subscribers.
_REGISTRY = {"aes": _build_aes}


__all__ = ["VaultCipher", "AesGcmCipher", "KeyFileProvider", "build_cipher"]
