"""SecretStore — the encrypted vault of known secret *values* + their labels.

The redaction policy (:func:`mote.common.secrets.policy.redact`) needs one thing:
a map of ``{plaintext value -> placeholder label}``. This store is the single
source of truth for that map, assembled from three tiers that all round-trip to a
real value on write (so a later PreToolUse *restore* direction can reverse it):

* **config section** (``<secret:llm.api_key>``) — secret string leaves harvested
  from a ``config.yaml`` by the config center's own
  :func:`~mote.common.config.diagnostics._is_secret` rule (leaf ∈
  ``CREDENTIAL_DENYLIST`` or contains key/secret/token/password/jwt), so there is
  **zero new detection logic**. Auto-synced into the vault and re-synced *hot*
  when the config file's mtime changes — editing ``config.yaml`` at runtime makes
  its api_key redact without a restart.
* **user section** (``<agent-vault:<key>>``) — named secrets uploaded via the CLI
  ``<secret name="KEY">VALUE</secret>`` mechanism, persisted (encrypted) to disk so
  they survive restarts.
* **session tier** (``<agent-vault:<key>>``) — anonymous secrets uploaded via
  ``<secret>VALUE</secret>``, held only in memory for the life of the process
  (never written to disk).

The on-disk vault is a **two-section** document ``{"config": {...}, "secrets":
{...}}`` encrypted whole by a :class:`~mote.common.secrets.cipher.VaultCipher`.
Every write is *section-isolated* — read-modify-write of the decrypted document,
replacing only the target section, then an atomic ``tmp + os.replace`` — so
re-syncing the config section can never clobber the user's named secrets and vice
versa. All reads fail **open**: an unreadable / undecryptable / malformed vault
yields an empty document, so a broken file means fewer known secrets, never a
crashed turn (redaction is disclosure hygiene, not a containment boundary).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from mote.common.config.diagnostics import _is_secret
from mote.common.const.paths import CONFIG_ROOT
from mote.common.secrets.cipher import VaultCipher

# The user vault file, in the config path (~/.mote/), never in a project tree.
_SECRETS_FILE = "secrets.json"

_CONFIG_SECTION = "config"
_SECRETS_SECTION = "secrets"


def secrets_path() -> Path:
    """The on-disk path of the encrypted vault file (``~/.mote/secrets.json``)."""
    return CONFIG_ROOT / _SECRETS_FILE


class SecretStore:
    """The encrypted, three-tier collection of known secret values → labels."""

    def __init__(
        self,
        cipher: VaultCipher,
        *,
        vault_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self._cipher = cipher
        self._vault_path = Path(vault_path) if vault_path is not None else secrets_path()
        #: The ``config.yaml`` whose secret leaves seed the config section. ``None``
        #: disables config auto-sync (unit tests that exercise only user/session).
        self._config_path = Path(config_path) if config_path is not None else None

        # Disk-backed tiers (mirrors of the two vault sections).
        self._config_section: Dict[str, str] = {}  # {dotted_path: value}
        self._user_section: Dict[str, str] = {}  # {key: value}
        # In-memory-only tier — anonymous session uploads, never persisted.
        self._session: Dict[str, str] = {}  # {key: value}

        # mtimes we last synced from, so ``refresh`` can skip untouched files.
        self._config_mtime: Optional[float] = None
        self._vault_mtime: Optional[float] = None

        self._load()  # decrypt whatever is already on disk (fail-open)
        self.refresh()  # initial config harvest + mtime baseline

    # -- reads --------------------------------------------------------------

    def as_map(self) -> Dict[str, str]:
        """Return the merged ``{value: label}`` map for the redaction policy.

        Refreshes first (cheap mtime stats), so a caller on the redaction hot
        path always sees a config edit / external vault write without an explicit
        reload. Later tiers overwrite earlier ones on a value clash (session wins
        over user wins over config) — the label is cosmetic, the masking is the
        same either way.
        """
        self.refresh()
        merged: Dict[str, str] = {}
        for dotted, value in self._config_section.items():
            if value:
                merged[value] = f"<secret:{dotted}>"
        for key, value in self._user_section.items():
            if value:
                merged[value] = f"<agent-vault:{key}>"
        for key, value in self._session.items():
            if value:
                merged[value] = f"<agent-vault:{key}>"
        return merged

    def __len__(self) -> int:
        return len(self._config_section) + len(self._user_section) + len(self._session)

    # -- writes -------------------------------------------------------------

    def add_user_secret(self, key: str, value: str) -> str:
        """Persist a named secret to the encrypted vault; return its label.

        Section-isolated write: only the ``secrets`` section is rewritten, so the
        auto-synced config section is preserved.
        """
        self._user_section[key] = value
        self._write_section(_SECRETS_SECTION, self._user_section)
        return f"<agent-vault:{key}>"

    def add_session_secret(self, value: str, *, key: Optional[str] = None) -> str:
        """Register an in-memory-only secret (anonymous upload); return its label.

        Never touches disk. A missing ``key`` gets a fresh opaque one so distinct
        anonymous uploads keep distinct labels within the session.
        """
        key = key or f"session-{uuid.uuid4().hex[:8]}"
        self._session[key] = value
        return f"<agent-vault:{key}>"

    def write_config_section(self, mapping: Dict[str, str]) -> None:
        """Replace the vault's config section with ``{dotted_path: value}``.

        Section-isolated (never clobbers the user's named secrets). Public so the
        seeder / a future admin path can drive it; :meth:`refresh` calls it when
        the config file changes.
        """
        self._write_section(_CONFIG_SECTION, mapping)

    # -- lifecycle ----------------------------------------------------------

    def refresh(self) -> None:
        """Lazily re-sync from disk by mtime — config reseed, then vault reload.

        Cheap when nothing changed (two ``stat`` calls). When ``config.yaml``
        changed, its secret leaves are re-harvested and rewritten into the config
        section (auto-sync, hot). When the vault file changed underneath us (an
        external write, or our own section write), the disk-backed tiers are
        reloaded. The in-memory session tier is untouched by either.
        """
        self._reseed_config_if_changed()
        self._reload_vault_if_changed()

    def _reseed_config_if_changed(self) -> None:
        if self._config_path is None:
            return
        mtime = _mtime(self._config_path)
        if mtime is None or mtime == self._config_mtime:
            return
        harvested = self._harvest_config_file()
        self.write_config_section(harvested)
        self._config_mtime = mtime

    def _reload_vault_if_changed(self) -> None:
        mtime = _mtime(self._vault_path)
        if mtime is None or mtime == self._vault_mtime:
            return
        self._load()
        self._vault_mtime = mtime

    # -- config harvest -----------------------------------------------------

    def _harvest_config_file(self) -> Dict[str, str]:
        """Parse the config file and collect ``{dotted_path: value}`` secret leaves."""
        if self._config_path is None:
            return {}
        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError):
            return {}
        harvested: Dict[str, str] = {}
        _harvest(data, "", harvested)
        return harvested

    # -- vault I/O ----------------------------------------------------------

    def _load(self) -> None:
        """Decrypt the vault (fail-open) and mirror its two sections in memory."""
        data = self._read_vault()
        config = data.get(_CONFIG_SECTION)
        secrets = data.get(_SECRETS_SECTION)
        self._config_section = _string_map(config)
        self._user_section = _string_map(secrets)

    def _read_vault(self) -> Dict[str, Any]:
        """Return the decrypted vault document, or ``{}`` on any failure."""
        try:
            blob = self._vault_path.read_bytes()
        except OSError:
            return {}
        try:
            plain = self._cipher.decrypt(blob)
            data = json.loads(plain.decode("utf-8"))
        except Exception:  # noqa: BLE001 — wrong key / tamper / malformed → empty (fail-open)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_section(self, section: str, mapping: Dict[str, str]) -> None:
        """Read-modify-write one section of the encrypted vault, atomically."""
        data = self._read_vault()
        data[section] = dict(mapping)
        self._atomic_write(data)
        # Mirror the just-written section so a caller sees it without a reload,
        # and record the fresh vault mtime so ``refresh`` does not reload our own
        # write back over the (identical) in-memory state.
        if section == _CONFIG_SECTION:
            self._config_section = _string_map(mapping)
        elif section == _SECRETS_SECTION:
            self._user_section = _string_map(mapping)
        self._vault_mtime = _mtime(self._vault_path)

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        blob = self._cipher.encrypt(json.dumps(data).encode("utf-8"))
        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._vault_path.with_name(self._vault_path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        os.replace(tmp, self._vault_path)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _string_map(node: Any) -> Dict[str, str]:
    """Coerce a decoded section into a ``{str: str}`` map (drop non-string leaves)."""
    if not isinstance(node, dict):
        return {}
    return {str(k): v for k, v in node.items() if isinstance(v, str)}


def _harvest(node: Any, prefix: str, out: Dict[str, str]) -> None:
    """Recursively collect secret string leaves keyed by dotted path.

    A leaf is secret iff :func:`_is_secret` says its dotted path is — the exact
    rule the config center uses for its redacted dump, reused only here (the
    seeder), never on the redaction hot path.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                _harvest(value, dotted, out)
            elif isinstance(value, str) and _is_secret(dotted):
                out[dotted] = value
    elif isinstance(node, list):
        # List items keep the parent path, so leaf-based _is_secret still applies.
        for item in node:
            _harvest(item, prefix, out)


__all__ = ["SecretStore", "secrets_path"]
