"""Runtime encrypted vault of known secret values and labels.

The redaction policy (:func:`mote.runtime.secrets.policy.redact`) needs one thing:
a map of ``{plaintext value -> placeholder label}``. This store is the single
source of truth for that map, assembled from three tiers that all round-trip to a
real value on write (so a later PreToolUse *restore* direction can reverse it):

* **config section** (``<secret:llm.api_key>``) — secret string leaves harvested
  from a ``config.yaml`` by the config center's own
  :func:`~mote.runtime.config.diagnostics._is_secret` rule (leaf ∈
  ``CREDENTIAL_DENYLIST`` or contains key/secret/token/password/jwt), so there is
  **zero new detection logic**. Auto-synced into the vault and re-synced *hot*
  when the config file's mtime changes — editing ``config.yaml`` at runtime makes
  its api_key redact without a restart.
* **file section** (``<agent-vault:<key>>``) — a plaintext, **human-edited**
  ``~/.mote/secrets_config.json`` (a flat ``{name: value}`` map). This is the one
  tier a person configures by hand: edit the JSON to add/rotate a named secret and
  it is (re)encrypted into the vault on the next refresh (hot, by mtime); *remove*
  an entry (or delete the whole file) and it is dropped from the vault too. It is a
  **full replacement** of the section — the file is the source of truth, so both
  add and delete propagate. Unlike the config tier it needs no ``_is_secret``
  heuristic (every entry is explicitly a secret) and unlike the CLI upload tier it
  is declarative (a file you version/manage, not an inline prompt span).
* **user section** (``<agent-vault:<key>>``) — named secrets uploaded via the CLI
  ``<secret name="KEY">VALUE</secret>`` mechanism, persisted (encrypted) to disk so
  they survive restarts.
* **session tier** (``<agent-vault:<key>>``) — anonymous secrets uploaded via
  ``<secret>VALUE</secret>``, held only in memory for the life of the process
  (never written to disk).

The on-disk vault is a **three-section** document ``{"config": {...}, "file":
{...}, "secrets": {...}}`` encrypted whole by a
:class:`~mote.runtime.secrets.cipher.VaultCipher`.
Every write is *section-isolated* — read-modify-write of the decrypted document,
replacing only the target section, then an atomic ``tmp + os.replace`` — so
re-syncing the config section can never clobber the user's named secrets and vice
versa. Missing files mean an empty vault; unreadable, undecryptable, or malformed
files fail loud so domain disclosure policies can fail closed instead of silently
treating damaged protection state as empty.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from mote.runtime.config.diagnostics import _is_secret
from mote.runtime.paths import CONFIG_ROOT
from mote.runtime.secrets.cipher import VaultCipher

# The user vault file, in the config path (~/.mote/), never in a project tree.
_SECRETS_FILE = "secrets.json"
# The plaintext, human-edited named-secret file (source of the "file" section).
_SECRETS_CONFIG_FILE = "secrets_config.json"

_CONFIG_SECTION = "config"
_FILE_SECTION = "file"
_SECRETS_SECTION = "secrets"

# Sentinel distinguishing "never synced" from "synced, file was absent (mtime None)"
# so the file section is reconciled once on construct even when the file is missing
# (a stale encrypted entry from a since-deleted file is then cleared on startup).
_UNSET = object()


def secrets_path() -> Path:
    """The on-disk path of the encrypted vault file (``~/.mote/secrets.json``)."""
    return CONFIG_ROOT / _SECRETS_FILE


def secrets_config_path() -> Path:
    """The plaintext, human-edited named-secret file (``~/.mote/secrets_config.json``)."""
    return CONFIG_ROOT / _SECRETS_CONFIG_FILE


class SecretStore:
    """The encrypted, three-tier collection of known secret values → labels."""

    def __init__(
        self,
        cipher: VaultCipher,
        *,
        vault_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
        secrets_config_file: Optional[Path] = None,
    ) -> None:
        self._cipher = cipher
        self._vault_path = Path(vault_path) if vault_path is not None else secrets_path()
        #: The ``config.yaml`` whose secret leaves seed the config section. ``None``
        #: disables config auto-sync (unit tests that exercise only user/session).
        self._config_path = Path(config_path) if config_path is not None else None
        #: The plaintext human-edited named-secret file seeding the file section.
        #: Defaults to ``~/.mote/secrets_config.json``; pass a path to relocate.
        self._secrets_config_path = (
            Path(secrets_config_file) if secrets_config_file is not None else secrets_config_path()
        )

        # Disk-backed tiers (mirrors of three vault sections).
        self._config_section: Dict[str, str] = {}  # {dotted_path: value}
        self._file_section: Dict[str, str] = {}  # {key: value} — human-edited file
        self._user_section: Dict[str, str] = {}  # {key: value}
        # In-memory-only tier — anonymous session uploads, never persisted.
        self._session: Dict[str, str] = {}  # {key: value}

        # mtimes we last synced from, so ``refresh`` can skip untouched files.
        self._config_mtime: Optional[float] = None
        # _UNSET (not None) so the file section reconciles once on construct even
        # when the file is absent — clearing a stale entry left by a deleted file.
        self._file_mtime: Any = _UNSET
        self._vault_mtime: Optional[float] = None
        # Construction is deliberately I/O-free.  Runtime assembly may create a
        # store while it is only resolving the component graph; touching the
        # vault here would turn an ordinary property read into filesystem I/O.
        # The first operation that actually needs durable state prepares the
        # store, and the runtime may call prepare() explicitly during startup.
        self._prepared = False

    def prepare(self) -> None:
        """Load and reconcile durable tiers once; idempotent.

        Kept synchronous because the underlying storage is currently local and
        all existing hot-refresh operations are synchronous.  This explicit
        lifecycle boundary ensures ``__init__`` remains a pure in-memory step.
        """
        if self._prepared:
            return
        # Mark first: refresh() is also the public lazy-entry point and must not
        # recurse while performing the initial reconciliation.
        self._prepared = True
        self._load()
        self.refresh()

    # -- reads --------------------------------------------------------------

    def as_map(self) -> Dict[str, str]:
        """Return the merged ``{value: label}`` map for the redaction policy.

        Refreshes first (cheap mtime stats), so a caller on the redaction hot
        path always sees a config / secrets_config edit / external vault write
        without an explicit reload. Later tiers overwrite earlier ones on a value
        clash (session > user > file > config) — the label is cosmetic, the
        masking is the same either way.
        """
        self.refresh()
        merged: Dict[str, str] = {}
        for dotted, value in self._config_section.items():
            if value:
                merged[value] = f"<secret:{dotted}>"
        for key, value in self._file_section.items():
            if value:
                merged[value] = f"<agent-vault:{key}>"
        for key, value in self._user_section.items():
            if value:
                merged[value] = f"<agent-vault:{key}>"
        for key, value in self._session.items():
            if value:
                merged[value] = f"<agent-vault:{key}>"
        return merged

    def labels(self) -> Dict[str, str]:
        """Return the ``{name: placeholder}`` map of NAMED secrets for discovery.

        The forward complement of :meth:`as_map` (value→label) and :meth:`get`
        (key→value): this exposes only the *keys* a model may reference, each
        paired with the exact placeholder it must write — and never a value. The
        config tier is keyed by dotted path → ``<secret:llm.api_key>``; the file
        and user tiers by name → ``<agent-vault:key>``. The **session tier is
        excluded** — its keys are random ``session-<uuid>`` strings, nothing a
        model could meaningfully reference.

        This is disclosure-safe: the names are already broadcast to the model as
        redaction placeholders (a masked value round-trips as ``<agent-vault:k>``
        in history), so enumerating them adds no leakage — and the value only
        ever flows out through :meth:`get`, never here. Refreshes first (cheap
        mtime stats) so a hot ``secrets_config.json`` edit is reflected.
        """
        self.refresh()
        out: Dict[str, str] = {}
        for dotted, value in self._config_section.items():
            if value:
                out[dotted] = f"<secret:{dotted}>"
        for key, value in self._file_section.items():
            if value:
                out[key] = f"<agent-vault:{key}>"
        for key, value in self._user_section.items():
            if value:
                out[key] = f"<agent-vault:{key}>"
        return out

    def get(self, key: str) -> Optional[str]:
        """Return a secret *value* by its key, or ``None`` if unknown.

        The inverse lookup of :meth:`as_map` (which is value→label): this
        resolves a *named* secret to its plaintext value for a trusted consumer
        (the sandbox credential broker) that references a secret **by key** and
        must never see the model author the value. Refreshes first (cheap mtime
        stats) so a hot config / ``secrets_config.json`` edit is honoured.

        Tiers are scanned highest-precedence first (session > user > file), then
        the config section — which is keyed by *dotted path* not a bare name, so
        a bare ``key`` also matches its trailing dotted segment (``llm.api_key``
        matched by ``api_key``). An empty stored value is treated as absent
        (fail-closed: the broker builds no partial credential).
        """
        self.refresh()
        for tier in (self._session, self._user_section, self._file_section):
            value = tier.get(key)
            if value:
                return value
        # Config section is keyed by dotted path; match the full path or a
        # trailing segment so a bare name resolves a nested config secret.
        for dotted, value in self._config_section.items():
            if value and (dotted == key or dotted.rsplit(".", 1)[-1] == key):
                return value
        return None

    def __len__(self) -> int:
        self.prepare()
        return len(self._config_section) + len(self._file_section) + len(self._user_section) + len(self._session)

    # -- writes -------------------------------------------------------------

    def add_user_secret(self, key: str, value: str) -> str:
        """Persist a named secret to the encrypted vault; return its label.

        Section-isolated write: only the ``secrets`` section is rewritten, so the
        auto-synced config section is preserved.
        """
        self.prepare()
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

    def write_file_section(self, mapping: Dict[str, str]) -> None:
        """Replace the vault's file section with the human-edited ``{name: value}``.

        Section-isolated (never clobbers config-auto-sync or the CLI user tier).
        A **full replacement**, so a name dropped from ``secrets_config.json`` is
        dropped from the vault too. :meth:`refresh` calls it when that file's
        mtime changes; public so an admin/seed path can drive it directly.
        """
        self._write_section(_FILE_SECTION, mapping)

    # -- lifecycle ----------------------------------------------------------

    def refresh(self) -> None:
        """Lazily re-sync from disk by mtime — config + file reseed, then reload.

        Cheap when nothing changed (a few ``stat`` calls). When ``config.yaml``
        changed, its secret leaves are re-harvested into the config section. When
        ``secrets_config.json`` changed (edit / delete of the whole file), its
        ``{name: value}`` map is re-encrypted into the file section as a full
        replacement (add and delete both propagate). When the vault file changed
        underneath us (an external write, or our own section write), the
        disk-backed tiers are reloaded. The in-memory session tier is untouched.
        """
        if not self._prepared:
            self.prepare()
            return
        self._reseed_config_if_changed()
        self._reseed_file_if_changed()
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

    def _reseed_file_if_changed(self) -> None:
        """Re-sync the file section from ``secrets_config.json`` by mtime.

        Unlike the config path this fires even when the file is *absent*: a
        ``None`` mtime that differs from the last-synced value (``_UNSET`` on the
        first call, or a real mtime after the file is deleted) reconciles the
        section to empty, so removing the file clears its vault entries too.
        """
        mtime = _mtime(self._secrets_config_path)
        if mtime == self._file_mtime:
            return
        harvested = self._harvest_secrets_config_file()
        # Skip the encrypt/write when nothing actually changed on disk (e.g. a
        # missing file that stays missing after the first reconcile).
        if harvested != self._file_section:
            self.write_file_section(harvested)
        self._file_mtime = mtime

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
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError("secret config could not be read") from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError("secret config is malformed") from exc
        if data is not None and not isinstance(data, dict):
            raise ValueError("secret config must contain a mapping")
        harvested: Dict[str, str] = {}
        _harvest(data, "", harvested)
        return harvested

    def _harvest_secrets_config_file(self) -> Dict[str, str]:
        """Parse the plaintext ``secrets_config.json`` into a ``{name: value}`` map.

        Every string leaf is a secret here (no ``_is_secret`` heuristic), so the
        file is a flat object of names to values. A missing / unreadable /
        file means an empty section. Unreadable, malformed, non-object, or
        non-string content fails loud so a bad boundary value cannot silently
        remove known protection state.
        """
        try:
            raw = self._secrets_config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError("secrets config could not be read") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError("secrets config is malformed") from exc
        if not isinstance(data, dict):
            raise ValueError("secrets config must contain a mapping")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()):
            raise ValueError("secrets config values must all be strings")
        return dict(data)

    # -- vault I/O ----------------------------------------------------------

    def _load(self) -> None:
        """Decrypt the vault and mirror its three sections in memory."""
        data = self._read_vault()
        self._config_section = _string_map(data.get(_CONFIG_SECTION))
        self._file_section = _string_map(data.get(_FILE_SECTION))
        self._user_section = _string_map(data.get(_SECRETS_SECTION))

    def _read_vault(self) -> Dict[str, Any]:
        """Return the decrypted vault document; only absence means empty."""
        try:
            blob = self._vault_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError("secret vault could not be read") from exc
        try:
            plain = self._cipher.decrypt(blob)
            data = json.loads(plain.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 -- crypto/codec boundary
            raise ValueError("secret vault is undecryptable or malformed") from exc
        if not isinstance(data, dict):
            raise ValueError("secret vault must contain a mapping")
        return data

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
        elif section == _FILE_SECTION:
            self._file_section = _string_map(mapping)
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


__all__ = ["SecretStore", "secrets_path", "secrets_config_path"]
