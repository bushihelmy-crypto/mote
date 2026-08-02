"""Crash-safe file-backed OAuth credential records."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialRecord,
    CredentialStore,
    record_from_dict,
    record_to_dict,
)


class FileCredentialStore(CredentialStore):
    def __init__(self, provider: str, base_dir: Path) -> None:
        super().__init__(provider, backend="file")
        self._dir = Path(base_dir).resolve()
        self._path = (self._dir / f"{self.subject}.json").resolve()
        if self._path.parent != self._dir:
            raise ValueError("OAuth credential path escaped storage root")

    @property
    def path(self) -> Path:
        return self._path

    def load_record(self) -> CredentialRecord | None:
        try:
            metadata = self._path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("OAuth credential record must be a 0600 regular file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise PermissionError("OAuth credential record owner mismatch")
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt OAuth credential record") from exc
        return record_from_dict(value, subject=self.subject, backend=self.backend)

    def commit(self, token: OAuthToken | None, *, expected_revision: int) -> CredentialRecord:
        current = self.load_record()
        actual_revision = current.revision if current is not None else 0
        if expected_revision != actual_revision:
            raise RuntimeError("OAuth credential revision conflict")
        generation = current.token_generation + 1 if current is not None else 1
        record = CredentialRecord(self.subject, self.backend, actual_revision + 1, generation, token)
        payload = json.dumps(record_to_dict(record), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._atomic_replace(payload)
        return record

    def _atomic_replace(self, payload: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._dir, 0o700)
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{self.subject}.", dir=self._dir)
        temporary = Path(raw_temp)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
            directory = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
