"""Encrypted, revisioned and crash-safe browser profile store."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from mote.contracts.browser import (
    BrowserProfileCommitReceipt,
    BrowserProfileConflictError,
    BrowserProfileError,
    BrowserProfileNotFoundError,
    BrowserProfileSnapshot,
    BrowserStorageState,
    decode_browser_storage_state,
)
from mote.runtime.secrets.cipher import VaultCipher

_SCHEMA = "mote.browser-profile/v1"


def canonical_profile_subject(name: str) -> tuple[str, str]:
    display = unicodedata.normalize("NFKC", name).strip()
    if not display or display in {".", ".."} or "/" in display or "\\" in display or "\x00" in display:
        raise BrowserProfileError("browser profile name is not a valid subject")
    return hashlib.sha256(display.encode("utf-8")).hexdigest(), display


def decode_storage_state(value: object) -> BrowserStorageState:
    try:
        return decode_browser_storage_state(value)
    except ValueError as error:
        raise BrowserProfileError(str(error)) from error


class BrowserProfileStore:
    _SUFFIX = ".profile"

    def __init__(self, cipher_factory: Callable[[], VaultCipher], *, root: Path) -> None:
        self._cipher_factory = cipher_factory
        self._cipher: VaultCipher | None = None
        self._root = Path(root)

    def _get_cipher(self) -> VaultCipher:
        if self._cipher is None:
            self._cipher = self._cipher_factory()
        return self._cipher

    def path_for(self, name: str) -> Path:
        subject_id, _ = canonical_profile_subject(name)
        return self._root / f"{subject_id}{self._SUFFIX}"

    @contextmanager
    def _claim(self, subject_id: str) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        lock_path = self._root / f"{subject_id}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self, name: str) -> BrowserProfileSnapshot:
        subject_id, display = canonical_profile_subject(name)
        path = self.path_for(name)
        try:
            token = path.read_bytes()
        except FileNotFoundError as exc:
            raise BrowserProfileNotFoundError(display) from exc
        try:
            raw = json.loads(self._get_cipher().decrypt(token).decode("utf-8"))
        except Exception as exc:
            raise BrowserProfileError("browser profile authentication or decoding failed") from exc
        if type(raw) is not dict or set(raw) != {
            "schema",
            "subject_id",
            "display_name",
            "revision",
            "content_digest",
            "storage_state",
        }:
            raise BrowserProfileError("browser profile envelope has an invalid shape")
        if raw["schema"] != _SCHEMA or raw["subject_id"] != subject_id or raw["display_name"] != display:
            raise BrowserProfileError("browser profile identity or schema mismatch")
        if type(raw["revision"]) is not int or raw["revision"] < 1 or type(raw["content_digest"]) is not str:
            raise BrowserProfileError("browser profile revision is invalid")
        state = decode_storage_state(raw["storage_state"])
        digest = self._digest(state)
        if digest != raw["content_digest"]:
            raise BrowserProfileError("browser profile content digest mismatch")
        return BrowserProfileSnapshot(subject_id, display, raw["revision"], digest, state)

    def save(
        self, name: str, storage_state: BrowserStorageState, *, expected_revision: int | None
    ) -> BrowserProfileCommitReceipt:
        if not isinstance(storage_state, BrowserStorageState):
            raise BrowserProfileError("storage state must be the canonical typed DTO")
        subject_id, display = canonical_profile_subject(name)
        with self._claim(subject_id):
            path = self.path_for(name)
            if path.exists():
                current = self.load(name)
                if expected_revision != current.revision:
                    raise BrowserProfileConflictError("browser profile revision changed")
                revision = current.revision + 1
            else:
                if expected_revision is not None:
                    raise BrowserProfileConflictError("browser profile does not exist at expected revision")
                revision = 1
            digest = self._digest(storage_state)
            envelope = {
                "schema": _SCHEMA,
                "subject_id": subject_id,
                "display_name": display,
                "revision": revision,
                "content_digest": digest,
                "storage_state": storage_state.to_payload(),
            }
            token = self._get_cipher().encrypt(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            self._atomic_replace(path, token)
            return BrowserProfileCommitReceipt(subject_id, revision, digest)

    def forget(self, name: str, *, expected_revision: int) -> None:
        subject_id, _ = canonical_profile_subject(name)
        with self._claim(subject_id):
            current = self.load(name)
            if current.revision != expected_revision:
                raise BrowserProfileConflictError("browser profile revision changed")
            self.path_for(name).unlink()
            self._fsync_directory()

    @staticmethod
    def _digest(state: BrowserStorageState) -> str:
        encoded = json.dumps(state.to_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _atomic_replace(self, path: Path, token: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self._root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory()
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _fsync_directory(self) -> None:
        fd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = ["BrowserProfileStore", "canonical_profile_subject", "decode_storage_state"]
