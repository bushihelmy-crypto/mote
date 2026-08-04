"""File secret vault bound to the canonical OAuth metadata repository."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

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
from mote.runtime.persistence.atomic import atomic_write
from mote.runtime.secrets.cipher import AesGcmCipher, DeferredVaultCipher, KeyFileProvider


class FileCredentialStore(CredentialStore):
    def __init__(self, provider: str, base_dir: Path) -> None:
        super().__init__(provider, backend="file")
        self._dir = Path(base_dir).resolve()
        self._vault_dir = (self._dir / "vault").resolve()
        self._metadata = CredentialMetadataRepository(self._dir, self.subject, self.backend)
        self._cipher = DeferredVaultCipher(lambda: AesGcmCipher(KeyFileProvider(self._dir / "vault.key").key()))
        if self._vault_dir.parent != self._dir:
            raise ValueError("OAuth credential path escaped storage root")

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
        path = self._secret_path(metadata.secret_ref)
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise PermissionError("OAuth secret generation must be a 0600 regular file")
            token = OAuthToken.model_validate_json(self._cipher.decrypt(path.read_bytes()))
        except FileNotFoundError as exc:
            raise RuntimeError("OAuth metadata references missing secret material") from exc
        except ValidationError as exc:
            raise ValueError("OAuth secret material is invalid") from exc
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
        ref = SecretRef(self.backend, f"{self.subject}.{generation}.secret", generation)
        self._vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._vault_dir, 0o700)
        secret_path = self._secret_path(ref)
        atomic_write(secret_path, self._cipher.encrypt(token.model_dump_json().encode()), mode=0o600)
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

    def _secret_path(self, ref: SecretRef) -> Path:
        path = (self._vault_dir / ref.key).resolve()
        if ref.backend != self.backend or path.parent != self._vault_dir:
            raise ValueError("OAuth SecretRef escaped or targets another vault")
        return path

    def _erase(self, ref: SecretRef) -> None:
        try:
            self._secret_path(ref).unlink()
        except FileNotFoundError:
            return
        descriptor = os.open(self._vault_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["FileCredentialStore"]
