"""BrowserProfileStore — durable, encrypted browser-login persistence.

A *profile* is a named, on-disk copy of a Playwright ``storage_state``
(``{cookies, origins}``) — the whole logged-in session for a browser. Where the
session-resume path keeps ``storage_state`` only for the lifetime of one session
(and, before this store, wrote it *plaintext* into ``rollout.jsonl``), a profile
promotes that login to a **durable identity** that outlives any single session:
once a role logs in under a profile name, later sessions reuse it with no
re-login (login ladder rung L0 — "reuse the persisted profile").

Security: the ``storage_state`` carries session cookies, so it is stored
**encrypted at rest**, reusing the very same AES-256-GCM cipher + ``vault.key``
that protects the secret vault (via an injected :class:`VaultCipher`). Files are
created ``0600`` (owner-only) from the start. Because the key is masked from the
sandbox, an encrypted profile is cryptographically useless to a confined command
even if it could read the bytes.

Best-effort by contract: every read/write failure (missing dir, corrupt file,
wrong key, unwritable home) is logged and swallowed — a profile problem yields a
clean ephemeral browser, never a broken turn. The cipher itself is built lazily
(on first load/save) via an injected factory, so a role that never uses a
profile never even generates the vault key.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from mote.common.const.paths import browser_profiles_dir
from mote.common.logs import logger
from mote.common.secrets.cipher import VaultCipher

#: Profile names come from a config knob (``role_schema.browser_profile``), but
#: they still name a file — sanitize to a conservative slug so a stray value can
#: never traverse out of the profile directory or collide with metadata files.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(name: str) -> str:
    """Reduce a profile name to a filesystem-safe slug (no path traversal)."""
    slug = _SAFE_NAME_RE.sub("_", name.strip())
    slug = slug.strip("._")  # never a hidden/relative-looking name
    return slug


class BrowserProfileStore:
    """Encrypted, durable store of named Playwright ``storage_state`` profiles.

    One file per profile under ``~/.mote/browser_profiles/<slug>.profile``, the
    JSON ``storage_state`` encrypted with the injected :class:`VaultCipher`. The
    cipher is resolved lazily through ``cipher_factory`` so construction is free
    (no key generated) until a profile is actually loaded or saved.
    """

    #: Extension for an encrypted profile blob (distinct from plain ``.json`` so
    #: the file is never mistaken for a readable document).
    _SUFFIX = ".profile"

    def __init__(
        self,
        cipher_factory: Callable[[], VaultCipher],
        *,
        root: Optional[Path] = None,
    ) -> None:
        self._cipher_factory = cipher_factory
        self._cipher: Optional[VaultCipher] = None
        self._root = Path(root) if root is not None else browser_profiles_dir()

    # --- internals ---------------------------------------------------------

    def _get_cipher(self) -> VaultCipher:
        """Build (once) and cache the vault cipher — lazy, key generated on demand."""
        if self._cipher is None:
            self._cipher = self._cipher_factory()
        return self._cipher

    def path_for(self, name: str) -> Path:
        """The on-disk path a profile *name* maps to (may not yet exist)."""
        return self._root / f"{_slug(name)}{self._SUFFIX}"

    # --- public API (all best-effort) --------------------------------------

    def load(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the decrypted ``storage_state`` for *name*, or ``None``.

        ``None`` on any miss/failure (no such profile, unreadable file, wrong
        key, malformed JSON) — the caller then launches a clean session.
        """
        if not name:
            return None
        path = self.path_for(name)
        try:
            token = path.read_bytes()
        except OSError:
            return None  # no such profile yet — normal on first use
        try:
            plaintext = self._get_cipher().decrypt(token)
            data = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — corrupt/wrong-key => clean start
            logger.warning(f"BrowserProfileStore: could not load profile {name!r}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def save(self, name: str, storage_state: Optional[Dict[str, Any]]) -> None:
        """Encrypt and persist *storage_state* under *name* (best-effort).

        A ``None`` or empty ``storage_state`` is ignored (nothing to persist);
        an existing profile is left untouched rather than clobbered with empty.
        """
        if not name or not storage_state:
            return
        path = self.path_for(name)
        try:
            plaintext = json.dumps(storage_state).encode("utf-8")
            token = self._get_cipher().encrypt(plaintext)
        except Exception as exc:  # noqa: BLE001 — serialize/encrypt failure is non-fatal
            logger.warning(f"BrowserProfileStore: could not encrypt profile {name!r}: {exc}")
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            # Owner-only from creation (like vault.key): open 0600 rather than
            # write-then-chmod so cookies are never briefly world-readable.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token)
            finally:
                os.close(fd)
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning(f"BrowserProfileStore: could not write profile {name!r}: {exc}")

    def forget(self, name: str) -> None:
        """Delete the profile named *name* if present (best-effort)."""
        if not name:
            return
        try:
            self.path_for(name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"BrowserProfileStore: could not forget profile {name!r}: {exc}")


__all__ = ["BrowserProfileStore"]
