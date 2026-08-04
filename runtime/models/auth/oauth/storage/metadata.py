"""Canonical fenced file owner for OAuth metadata and borrow evidence."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from mote.runtime.models.auth.oauth.storage.base import (
    CredentialMetadata,
    CredentialState,
    CredentialSubjectId,
    CredentialUse,
    SecretRef,
    metadata_from_dict,
    metadata_to_dict,
)
from mote.runtime.persistence.atomic import atomic_write

OAUTH_BORROW_SCHEMA = "mote.oauth-borrow/v1"
MAX_BORROWS_PER_SUBJECT = 64
MAX_BORROW_DURATION = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class BorrowEvidence:
    borrow_id: str
    issued_at: datetime
    expires_at: datetime


class CredentialMetadataRepository:
    """One CAS/fence and borrow ledger independent of the selected secret vault."""

    def __init__(self, root: Path, subject: CredentialSubjectId, backend: str) -> None:
        self._root = Path(root).resolve()
        self._subject = subject
        self._backend = backend
        self._path = (self._root / f"{subject}.metadata.json").resolve()
        self._borrow_dir = (self._root / "borrows" / str(subject)).resolve()
        self._lock = FileLock(str(self._root / f"{subject}.store.lock"))
        if self._path.parent != self._root or self._borrow_dir.parent.parent != self._root:
            raise ValueError("OAuth metadata path escaped storage root")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> CredentialMetadata | None:
        try:
            metadata = self._path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("OAuth credential metadata must be a 0600 regular file")
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt OAuth credential metadata") from exc
        return metadata_from_dict(value, subject=self._subject, backend=self._backend)

    def publish(self, ref: SecretRef, *, expected_revision: int) -> tuple[CredentialMetadata, SecretRef | None]:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            current = self.load()
            actual = 0 if current is None else current.revision
            if actual != expected_revision:
                raise RuntimeError("OAuth credential revision conflict")
            if ref.backend != self._backend:
                raise ValueError("OAuth secret backend binding mismatch")
            record = CredentialMetadata(
                self._subject,
                self._backend,
                actual + 1,
                ref.generation,
                CredentialState.ACTIVE,
                ref,
                False if current is None else current.legal_hold,
            )
            self._write(record)
            self._revoke_borrows()
            return record, current.secret_ref if current is not None else None

    def transition(
        self,
        state: CredentialState,
        *,
        expected_revision: int,
    ) -> tuple[CredentialMetadata, SecretRef | None]:
        if state in {CredentialState.ACTIVE, CredentialState.REFRESHING}:
            raise ValueError("OAuth material state requires publish")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            current = self.load()
            if current is None or current.revision != expected_revision:
                raise RuntimeError("OAuth credential revision conflict")
            retain = state in {
                CredentialState.REVOCATION_PENDING,
                CredentialState.IN_DOUBT,
                CredentialState.OWNER_ACTION_REQUIRED,
            }
            record = CredentialMetadata(
                self._subject,
                self._backend,
                current.revision + 1,
                current.secret_generation,
                state,
                current.secret_ref if retain else None,
                current.legal_hold,
            )
            self._write(record)
            if not retain:
                self._revoke_borrows()
            return record, None if retain else current.secret_ref

    def set_legal_hold(self, enabled: bool, *, expected_revision: int) -> CredentialMetadata:
        if type(enabled) is not bool:
            raise ValueError("OAuth legal hold must be boolean")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            current = self.load()
            if current is None or current.revision != expected_revision:
                raise RuntimeError("OAuth credential revision conflict")
            if current.legal_hold is enabled:
                return current
            record = CredentialMetadata(
                current.subject,
                current.backend,
                current.revision + 1,
                current.secret_generation,
                current.state,
                current.secret_ref,
                enabled,
            )
            self._write(record)
            return record

    def register_borrow(
        self,
        metadata: CredentialMetadata,
        use: CredentialUse,
        *,
        expires_at: datetime,
    ) -> BorrowEvidence:
        now = datetime.now(timezone.utc)
        if (
            expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or not now < expires_at <= now + MAX_BORROW_DURATION
        ):
            raise ValueError("OAuth borrow expiry exceeds the hard operation bound")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            if self.load() != metadata:
                raise RuntimeError("OAuth credential generation changed during borrow")
            self._reap_expired_borrows(now)
            if len(tuple(self._borrow_dir.glob("*.json"))) >= MAX_BORROWS_PER_SUBJECT:
                raise RuntimeError("OAuth credential borrow capacity is exhausted")
            borrow_id = uuid4().hex
            marker = {
                "schema": OAUTH_BORROW_SCHEMA,
                "borrow_id": borrow_id,
                "subject": str(self._subject),
                "generation": metadata.secret_generation,
                "use_digest": use.digest,
                "issued_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            atomic_write(
                self._borrow_path(borrow_id),
                json.dumps(marker, sort_keys=True, separators=(",", ":")).encode(),
                mode=0o600,
            )
            return BorrowEvidence(borrow_id, now, expires_at)

    def release_borrow(self, borrow_id: str) -> None:
        with self._lock:
            try:
                self._borrow_path(borrow_id).unlink()
            except FileNotFoundError:
                return
            self._fsync_directory(self._borrow_dir)

    def _write(self, record: CredentialMetadata) -> None:
        payload = json.dumps(metadata_to_dict(record), sort_keys=True, separators=(",", ":")).encode()
        atomic_write(self._path, payload, mode=0o600)

    def _borrow_path(self, borrow_id: str) -> Path:
        if not borrow_id or not borrow_id.isalnum():
            raise ValueError("OAuth borrow identity is invalid")
        path = (self._borrow_dir / f"{borrow_id}.json").resolve()
        if path.parent != self._borrow_dir:
            raise ValueError("OAuth borrow path escaped its subject root")
        return path

    def _reap_expired_borrows(self, now: datetime) -> None:
        if not self._borrow_dir.exists():
            return
        for path in self._borrow_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_bytes())
                if type(raw) is not dict or raw.get("schema") != OAUTH_BORROW_SCHEMA:
                    raise ValueError
                expires_at = datetime.fromisoformat(raw["expires_at"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("OAuth borrow evidence is corrupt or unsupported") from exc
            if expires_at.tzinfo is None:
                raise ValueError("OAuth borrow evidence has no absolute expiry")
            if expires_at <= now:
                path.unlink()

    def _revoke_borrows(self) -> None:
        if not self._borrow_dir.exists():
            return
        for path in self._borrow_dir.glob("*.json"):
            path.unlink()
        self._fsync_directory(self._borrow_dir)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "BorrowEvidence",
    "CredentialMetadataRepository",
    "MAX_BORROWS_PER_SUBJECT",
    "MAX_BORROW_DURATION",
    "OAUTH_BORROW_SCHEMA",
]
