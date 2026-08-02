"""Optional authoritative OS-keyring OAuth credential store."""

from __future__ import annotations

import json

try:
    import keyring
except Exception as _keyring_import_error:  # noqa: BLE001 -- optional backend activation
    keyring = None
else:
    _keyring_import_error = None

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialRecord,
    CredentialStore,
    record_from_dict,
    record_to_dict,
)

_SERVICE = "mote-oauth-v1"


class KeyringCredentialStore(CredentialStore):
    def __init__(self, provider: str) -> None:
        super().__init__(provider, backend="keyring")
        if keyring is None:
            raise RuntimeError(f"keyring backend unavailable: {_keyring_import_error}")
        self._keyring = keyring

    def load_record(self) -> CredentialRecord | None:
        raw = self._keyring.get_password(_SERVICE, self.subject)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw:
            raise ValueError("corrupt OAuth keyring credential record")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("corrupt OAuth keyring credential record") from exc
        return record_from_dict(value, subject=self.subject, backend=self.backend)

    def commit(self, token: OAuthToken | None, *, expected_revision: int) -> CredentialRecord:
        current = self.load_record()
        actual_revision = current.revision if current is not None else 0
        if expected_revision != actual_revision:
            raise RuntimeError("OAuth credential revision conflict")
        generation = current.token_generation + 1 if current is not None else 1
        record = CredentialRecord(self.subject, self.backend, actual_revision + 1, generation, token)
        self._keyring.set_password(
            _SERVICE,
            self.subject,
            json.dumps(record_to_dict(record), sort_keys=True, separators=(",", ":")),
        )
        return record
