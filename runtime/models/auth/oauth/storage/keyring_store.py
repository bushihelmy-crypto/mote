"""OS-keyring secret vault bound to canonical fenced file metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

try:
    import keyring
    from keyring.errors import PasswordDeleteError
except Exception as _keyring_import_error:  # noqa: BLE001 -- optional backend activation
    keyring = None
    PasswordDeleteError = RuntimeError
else:
    _keyring_import_error = None

from pydantic import ValidationError

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialBorrow,
    CredentialMetadata,
    CredentialState,
    CredentialStore,
    CredentialUse,
    SecretRef,
)
from mote.runtime.models.auth.oauth.storage.metadata import CredentialMetadataRepository

_SECRET_SERVICE = "mote-oauth-secret-v2"


class KeyringCredentialStore(CredentialStore):
    """Keyring contains secret bytes only; file metadata remains authoritative."""

    def __init__(self, provider: str, base_dir: Path) -> None:
        super().__init__(provider, backend="keyring")
        if keyring is None:
            raise RuntimeError(f"keyring backend unavailable: {_keyring_import_error}")
        self._keyring = keyring
        self._metadata = CredentialMetadataRepository(Path(base_dir), self.subject, self.backend)

    @property
    def path(self) -> Path:
        return self._metadata.path

    def load_metadata(self) -> CredentialMetadata | None:
        return self._metadata.load()

    def borrow(self, use: CredentialUse, *, expires_at: datetime) -> CredentialBorrow | None:
        if use.provider != self.external_name:
            raise ValueError("OAuth credential borrow provider mismatch")
        metadata = self.load_metadata()
        if (
            metadata is None
            or metadata.state
            not in {
                CredentialState.ACTIVE,
                CredentialState.REFRESHING,
                CredentialState.REVOCATION_PENDING,
                CredentialState.IN_DOUBT,
                CredentialState.OWNER_ACTION_REQUIRED,
            }
            or metadata.secret_ref is None
        ):
            return None
        raw = self._keyring.get_password(_SECRET_SERVICE, metadata.secret_ref.key)
        if raw is None:
            raise RuntimeError("OAuth metadata references missing keyring material")
        try:
            token = OAuthToken.model_validate_json(raw)
        except ValidationError as exc:
            raise ValueError("OAuth keyring material is invalid") from exc
        evidence = self._metadata.register_borrow(metadata, use, expires_at=expires_at)
        return CredentialBorrow(
            evidence.borrow_id,
            metadata,
            token,
            use.digest,
            evidence.issued_at,
            evidence.expires_at,
        )

    def release_borrow(self, borrow: CredentialBorrow) -> None:
        if borrow.metadata.subject != self.subject:
            raise ValueError("OAuth borrow belongs to another credential subject")
        self._metadata.release_borrow(borrow.borrow_id)

    def publish(self, token: OAuthToken, *, expected_revision: int) -> CredentialMetadata:
        current = self.load_metadata()
        actual = 0 if current is None else current.revision
        if actual != expected_revision:
            raise RuntimeError("OAuth credential revision conflict")
        generation = (0 if current is None else current.secret_generation) + 1
        ref = SecretRef(self.backend, f"{self.subject}:{generation}", generation)
        self._keyring.set_password(_SECRET_SERVICE, ref.key, token.model_dump_json())
        try:
            record, retired = self._metadata.publish(ref, expected_revision=expected_revision)
        except BaseException:
            self._erase(ref)
            raise
        if retired is not None:
            self._erase(retired)
        return record

    def transition(self, state: CredentialState, *, expected_revision: int) -> CredentialMetadata:
        record, retired = self._metadata.transition(state, expected_revision=expected_revision)
        if retired is not None:
            self._erase(retired)
        return record

    def set_legal_hold(self, enabled: bool, *, expected_revision: int) -> CredentialMetadata:
        return self._metadata.set_legal_hold(enabled, expected_revision=expected_revision)

    def _erase(self, ref: SecretRef) -> None:
        if ref.backend != self.backend:
            raise ValueError("OAuth SecretRef targets another vault")
        try:
            self._keyring.delete_password(_SECRET_SERVICE, ref.key)
        except PasswordDeleteError:
            return


__all__ = ["KeyringCredentialStore"]
