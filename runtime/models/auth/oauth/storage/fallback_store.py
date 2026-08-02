"""One-time backend selection for OAuth credentials.

The selected backend is durable and authoritative for the subject.  Runtime
operation failures never cause per-call drift to the other backend.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import CredentialRecord, CredentialStore, credential_subject
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore
from mote.runtime.models.auth.oauth.storage.keyring_store import KeyringCredentialStore

_SELECTION_VERSION = 1


class FallbackCredentialStore(CredentialStore):
    def __init__(self, provider: str, base_dir: Path) -> None:
        super().__init__(provider, backend="fallback")
        root = Path(base_dir).resolve()
        selection_path = root / f"{self.subject}.backend.json"
        lock_path = root / f"{self.subject}.backend.lock"
        root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path)):
            selected = self._load_selection(selection_path)
            if selected is None:
                try:
                    selected_store: CredentialStore = KeyringCredentialStore(provider)
                    selected_store.load_record()
                    selected = "keyring"
                except ValueError:
                    raise
                except Exception:  # optional backend is unavailable at initialization
                    selected_store = FileCredentialStore(provider, root)
                    selected = "file"
                self._write_selection(
                    selection_path,
                    json.dumps(
                        {
                            "version": _SELECTION_VERSION,
                            "subject": self.subject,
                            "backend": selected,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            elif selected == "keyring":
                selected_store = KeyringCredentialStore(provider)
            else:
                selected_store = FileCredentialStore(provider, root)
        self.backend = selected
        self._selected = selected_store

    def _load_selection(self, path: Path) -> str | None:
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt OAuth backend selection") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "subject", "backend"}
            or type(value["version"]) is not int
            or value["version"] != _SELECTION_VERSION
            or value["subject"] != credential_subject(self.external_name)
            or value["backend"] not in {"file", "keyring"}
        ):
            raise ValueError("invalid OAuth backend selection")
        return value["backend"]

    def _write_selection(self, path: Path, payload: bytes) -> None:
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{self.subject}.backend.", dir=path.parent)
        temporary = Path(raw_temp)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load_record(self) -> CredentialRecord | None:
        return self._selected.load_record()

    def commit(self, token: OAuthToken | None, *, expected_revision: int) -> CredentialRecord:
        return self._selected.commit(token, expected_revision=expected_revision)
