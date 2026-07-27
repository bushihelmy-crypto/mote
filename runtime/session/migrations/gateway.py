"""Atomic whole-rollout migration at the session persistence boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

from mote.contracts.events import EventEnvelope, StreamId
from mote.contracts.fileops.events import FileHistoryImportedEvent
from mote.contracts.fileops.models import LockMode, LockSpec
from mote.runtime.events.journal import LocalEventJournal, encode_event_record
from mote.runtime.fileops.artifact_budgets import ARTIFACT_HARD_LIMIT_BYTES, ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.artifact_repository import ArtifactRepository
from mote.runtime.fileops.locking import JOURNAL_LOCK_LEVEL, HierarchicalLockManager
from mote.runtime.logging import log_class
from mote.runtime.session.codec import (
    SESSION_FACT_SCHEMA_VERSION,
    decode_session_event,
    legacy_occurred_at,
    migrated_event_id,
    session_stream_id,
    stable_event_type,
    unknown_legacy_event_type,
)
from mote.runtime.session.events import SCHEMA_VERSION, SESSION_EVENT_CLASSES, SESSION_META, SessionMetaEvent
from mote.runtime.session.migrations.v1 import import_v1_file_snapshot, parse_v1_file_snapshot

_V1_SCHEMA_VERSION = 1
_V1_FILE_SNAPSHOT = "file_snapshot"
_MAX_RECORD_BYTES = 256 * 1_024 * 1_024
_COPY_CHUNK_BYTES = 1_024 * 1_024


class SessionMigrationError(RuntimeError):
    """A rollout cannot be proved safe to replace with the current schema."""


@log_class(level="DEBUG", exclude={"ensure_current"})
class SessionSchemaGateway:
    """Upgrades one rollout before current runtime code can observe it."""

    def __init__(
        self,
        *,
        session_id: str,
        path: Path,
        schema_lock_root: Path,
        journal_lock_root: Path,
    ) -> None:
        self.session_id = session_id
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.legacy.backup")
        self.stream_id = StreamId(session_stream_id(session_id))
        lock_key = hashlib.sha256(os.fsencode(self.path.absolute())).hexdigest()
        self.lock_path = Path(schema_lock_root) / f"{lock_key}.lock"
        self.journal_lock_root = Path(journal_lock_root)
        self.journal_lock = LockSpec(
            JOURNAL_LOCK_LEVEL,
            lock_key,
            LockMode.EXCLUSIVE,
            f"session schema migration {session_id}",
        )

    def ensure_current(self) -> bool:
        """Migrate the whole log once; return whether it was replaced."""

        if not self.path.exists():
            return False
        journal_locks = HierarchicalLockManager(self.journal_lock_root)
        try:
            with self._migration_lock():
                with journal_locks.acquire_many((self.journal_lock,)):
                    if self._is_current(self.path):
                        return False
                    source_version = self._legacy_schema_version(self.path)
                    self.backup_path = self.path.with_name(f"{self.path.name}.schema-v{source_version}.backup")
                    self._ensure_backup()
                    repository = ArtifactRepository(
                        self.path.parent / "blobs",
                        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
                    )
                    temporary = self._write_migrated(repository)
                    try:
                        self._verify_current(temporary, repository)
                        os.replace(temporary, self.path)
                        self._fsync_directory(self.path.parent)
                    finally:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
                    return True
        except SessionMigrationError:
            raise
        except Exception as exc:
            raise SessionMigrationError(f"cannot migrate session rollout: {self.session_id}") from exc

    def _is_current(self, path: Path) -> bool:
        if self._is_envelope_stream(path):
            journal = LocalEventJournal(path, self.stream_id)
            report = journal.verify_committed(self.stream_id)
            if not report.valid:
                issue = report.issues[0]
                raise SessionMigrationError(f"session journal integrity failure at line {issue.line}: {issue.detail}")
            first = next(journal.iter_committed(self.stream_id), None)
            if first is None:
                raise SessionMigrationError("session journal has no metadata fact")
            event = decode_session_event(first)
            if not isinstance(event, SessionMetaEvent):
                raise SessionMigrationError("session metadata is not the first fact")
            if event.session_id != self.session_id:
                raise SessionMigrationError("session journal identity does not match its path")
            return True

        version: int | None = None
        has_meta = False
        for ordinal, record in self._iter_records(path):
            if ordinal == 1 and record["type"] == SESSION_META:
                self._validate_meta(record)
                has_meta = True
                version = record["payload"].get("schema_version")
                if type(version) is not int or version not in {
                    _V1_SCHEMA_VERSION,
                    2,
                    SCHEMA_VERSION,
                }:
                    raise SessionMigrationError(f"unsupported session schema version: {version!r}")
        return False

    def _write_migrated(self, repository: ArtifactRepository) -> Path:
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.migrate-",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(raw_path)
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                records = self._iter_records(self.path)
                first = next(records, None)
                has_meta = first is not None and first[1]["type"] == SESSION_META
                source_version = _V1_SCHEMA_VERSION
                if has_meta and first is not None:
                    source_version = first[1]["payload"].get("schema_version", _V1_SCHEMA_VERSION)
                recorded_at = datetime.now(timezone.utc)
                previous_checksum: str | None = None
                sequence = 0
                if not has_meta:
                    timestamp = first[1]["ts"] if first is not None else "1970-01-01T00:00:00"
                    sequence, previous_checksum = self._write_envelope(
                        stream,
                        {
                            "type": SESSION_META,
                            "ts": timestamp,
                            "payload": {
                                "session_id": self.session_id,
                                "schema_version": SCHEMA_VERSION,
                                "created_at": timestamp,
                            },
                        },
                        sequence=sequence,
                        previous_checksum=previous_checksum,
                        recorded_at=recorded_at,
                        source_version=source_version,
                        ordinal=0,
                    )
                if first is not None:
                    sequence, previous_checksum = self._write_envelope(
                        stream,
                        self._migrate_record(
                            first[0],
                            first[1],
                            repository,
                            meta=has_meta,
                        ),
                        sequence=sequence,
                        previous_checksum=previous_checksum,
                        recorded_at=recorded_at,
                        source_version=source_version,
                        ordinal=first[0],
                    )
                for ordinal, record in records:
                    sequence, previous_checksum = self._write_envelope(
                        stream,
                        self._migrate_record(
                            ordinal,
                            record,
                            repository,
                            meta=False,
                        ),
                        sequence=sequence,
                        previous_checksum=previous_checksum,
                        recorded_at=recorded_at,
                        source_version=source_version,
                        ordinal=ordinal,
                    )
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return temporary

    def _migrate_record(
        self,
        ordinal: int,
        record: dict,
        repository: ArtifactRepository,
        *,
        meta: bool,
    ) -> dict:
        if meta:
            payload = dict(record["payload"])
            payload["schema_version"] = SCHEMA_VERSION
            return {**record, "payload": payload}
        if record["type"] != _V1_FILE_SNAPSHOT:
            return record
        snapshot = parse_v1_file_snapshot(record["payload"])
        reservation = repository.reserve(
            snapshot.pre_size,
            f"session-schema-v1:{self.session_id}:{ordinal}",
            ARTIFACT_WRITE_TTL_SECONDS,
        )
        event = import_v1_file_snapshot(
            snapshot,
            session_id=self.session_id,
            source_ordinal=ordinal,
            recorded_at=record["ts"],
            session_dir=self.path.parent,
            repository=repository,
            reservation=reservation,
        )
        return {
            "type": event.type,
            "ts": record["ts"],
            "payload": event.payload(),
        }

    def _verify_current(
        self,
        path: Path,
        repository: ArtifactRepository,
    ) -> None:
        journal = LocalEventJournal(path, self.stream_id)
        report = journal.verify_committed(self.stream_id)
        if not report.valid:
            issue = report.issues[0]
            raise SessionMigrationError(
                f"migrated session journal failed verification at line {issue.line}: {issue.detail}"
            )
        envelopes = journal.iter_committed(self.stream_id)
        first = next(envelopes, None)
        if first is None:
            raise SessionMigrationError("migrated session journal has no metadata")
        meta = decode_session_event(first)
        if not isinstance(meta, SessionMetaEvent):
            raise SessionMigrationError("migrated session metadata is not first")
        if meta.session_id != self.session_id:
            raise SessionMigrationError("migrated session identity does not match its path")
        if meta.schema_version != SCHEMA_VERSION:
            raise SessionMigrationError("migrated session schema was not advanced")
        for envelope in envelopes:
            event = decode_session_event(envelope)
            if not isinstance(event, FileHistoryImportedEvent):
                continue
            try:
                if event.before is not None:
                    repository.verify(event.before)
            except Exception as exc:
                raise SessionMigrationError("migrated file history artifact failed verification") from exc

    def _iter_records(self, path: Path) -> Iterator[tuple[int, dict]]:
        with path.open("rb") as stream:
            line_number = 0
            while True:
                raw = stream.readline(_MAX_RECORD_BYTES + 1)
                if not raw:
                    return
                line_number += 1
                if len(raw) > _MAX_RECORD_BYTES:
                    raise SessionMigrationError(
                        f"session rollout record exceeds the migration bound at line {line_number}"
                    )
                if not raw.endswith(b"\n"):
                    raise SessionMigrationError(f"session rollout has a torn line at {line_number}")
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise SessionMigrationError(f"session rollout has invalid JSON at line {line_number}") from exc
                if (
                    type(record) is not dict
                    or set(record) != {"type", "ts", "payload"}
                    or type(record["type"]) is not str
                    or not record["type"]
                    or type(record["ts"]) is not str
                    or not record["ts"]
                    or type(record["payload"]) is not dict
                ):
                    raise SessionMigrationError(f"session rollout envelope is invalid at line {line_number}")
                if line_number > 1 and record["type"] == SESSION_META:
                    raise SessionMigrationError("session rollout metadata is not the first record")
                yield line_number, record

    def _validate_meta(self, record: dict) -> None:
        payload = record["payload"]
        if payload.get("session_id") != self.session_id:
            raise SessionMigrationError("session rollout identity does not match its path")

    def _ensure_backup(self) -> None:
        if self.backup_path.exists():
            self._verify_backup()
            return
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{self.backup_path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(raw_path)
        try:
            with self.path.open("rb") as source:
                with os.fdopen(fd, "wb", closefd=True) as destination:
                    while True:
                        chunk = source.read(_COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        destination.write(chunk)
                    destination.flush()
                    if hasattr(os, "fchmod"):
                        os.fchmod(destination.fileno(), stat.S_IRUSR)
                    os.fsync(destination.fileno())
            if not hasattr(os, "fchmod"):  # pragma: no cover - Windows
                temporary.chmod(stat.S_IRUSR)
            try:
                os.link(temporary, self.backup_path)
            except FileExistsError:
                pass
            self._verify_backup()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _verify_backup(self) -> None:
        if not self._files_equal(self.path, self.backup_path):
            raise SessionMigrationError("existing session migration backup differs from its source")
        if os.name == "posix" and stat.S_IMODE(self.backup_path.stat().st_mode) & 0o222:
            raise SessionMigrationError("existing session migration backup is not read-only")
        with self.backup_path.open("rb") as stream:
            os.fsync(stream.fileno())
        self._fsync_directory(self.backup_path.parent)

    @staticmethod
    def _files_equal(left: Path, right: Path) -> bool:
        with left.open("rb") as left_stream, right.open("rb") as right_stream:
            while True:
                left_chunk = left_stream.read(_COPY_CHUNK_BYTES)
                right_chunk = right_stream.read(_COPY_CHUNK_BYTES)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    def _write_envelope(
        self,
        stream,
        record: dict,
        *,
        sequence: int,
        previous_checksum: str | None,
        recorded_at: datetime,
        source_version: int,
        ordinal: int,
    ) -> tuple[int, str]:
        legacy_type = record["type"]
        payload = record["payload"]
        event_type = (
            stable_event_type(legacy_type)
            if legacy_type in SESSION_EVENT_CLASSES
            else unknown_legacy_event_type(legacy_type)
        )
        metadata = {
            "migration_source_schema": source_version,
            "legacy_event_type": legacy_type,
            "legacy_timestamp": record["ts"],
        }
        envelope = EventEnvelope(
            event_id=migrated_event_id(
                session_id=self.session_id,
                ordinal=ordinal,
                legacy_type=legacy_type,
                timestamp=record["ts"],
                payload=payload,
            ),
            event_type=event_type,
            schema_version=SESSION_FACT_SCHEMA_VERSION,
            stream_id=self.stream_id,
            sequence=sequence + 1,
            occurred_at=legacy_occurred_at(record["ts"]),
            recorded_at=recorded_at,
            payload=payload,
            session_id=self.session_id,
            run_id=self._payload_identity(payload, "run_id"),
            turn_id=self._payload_identity(payload, "turn_id"),
            metadata=metadata,
        )
        line, checksum = encode_event_record(envelope, previous_checksum)
        stream.write(line)
        return sequence + 1, checksum

    def _legacy_schema_version(self, path: Path) -> int:
        first = next(self._iter_records(path), None)
        if first is None or first[1]["type"] != SESSION_META:
            return _V1_SCHEMA_VERSION
        version = first[1]["payload"].get("schema_version", _V1_SCHEMA_VERSION)
        if type(version) is not int or version not in {
            _V1_SCHEMA_VERSION,
            2,
            SCHEMA_VERSION,
        }:
            raise SessionMigrationError(f"unsupported session schema version: {version!r}")
        return version

    @staticmethod
    def _payload_identity(payload: dict, name: str) -> str | None:
        value = payload.get(name)
        return value if type(value) is str and value else None

    @staticmethod
    def _is_envelope_stream(path: Path) -> bool:
        with path.open("rb") as stream:
            raw = stream.readline(_MAX_RECORD_BYTES + 1)
        if not raw:
            return False
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        return type(record) is dict and "format_version" in record

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows
                stream.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    stream.fileno(), msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
                )
            else:  # pragma: no cover - unsupported platform
                raise SessionMigrationError("no process lock is available")
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    stream.seek(0)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        stream.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                    )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = ["SessionMigrationError", "SessionSchemaGateway"]
