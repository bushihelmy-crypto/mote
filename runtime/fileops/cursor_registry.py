"""Durable, epoch-fenced cursor leases shared by Read and Search."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.content.identity import ContentDigest, ContentIdentity
from mote.contracts.file.codec import path_to_dict, snapshot_from_dict, snapshot_to_dict
from mote.contracts.file.errors import ReadCursorError
from mote.contracts.file.identity import FileSnapshot, PathToken

_SCHEMA_VERSION = 1
_MAX_INTEGER = (1 << 63) - 1
_MAX_NAMESPACE_BYTES = 64
_MAX_PINNED_ARTIFACTS = 4_096
_MAX_OBSERVED_SNAPSHOT_BYTES = 1_024 * 1_024
_TOKEN_BYTES = 32
_TOKEN_CHARACTERS = 43
_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
DEFAULT_CURSOR_IDLE_TTL_NS = 30 * 60 * 1_000_000_000
DEFAULT_CURSOR_HARD_TTL_NS = 24 * 60 * 60 * 1_000_000_000

_TABLE_COLUMNS = {
    "timeline": (
        ("singleton", "INTEGER", 0, None, 1),
        ("epoch", "INTEGER", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("pin_revision", "INTEGER", 1, None, 0),
        ("updated_at_ns", "INTEGER", 1, None, 0),
    ),
    "timeline_transitions": (
        ("epoch", "INTEGER", 0, None, 1),
        ("prior_epoch", "INTEGER", 1, None, 0),
        ("occurred_at_ns", "INTEGER", 1, None, 0),
    ),
    "cursor_leases": (
        ("lease_id", "TEXT", 0, None, 1),
        ("namespace", "TEXT", 1, None, 0),
        ("epoch", "INTEGER", 1, None, 0),
        ("root_digest", "TEXT", 1, None, 0),
        ("root_size", "INTEGER", 1, None, 0),
        ("secret", "BLOB", 1, None, 0),
        ("issued_at_ns", "INTEGER", 1, None, 0),
        ("expires_at_ns", "INTEGER", 1, None, 0),
        ("hard_expires_at_ns", "INTEGER", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("released_at_ns", "INTEGER", 0, None, 0),
    ),
    "cursor_pins": (
        ("lease_id", "TEXT", 1, None, 1),
        ("ordinal", "INTEGER", 1, None, 2),
        ("digest", "TEXT", 1, None, 0),
        ("size", "INTEGER", 1, None, 0),
    ),
    "cursor_grants": (
        ("grant_id", "TEXT", 0, None, 1),
        ("lease_id", "TEXT", 1, None, 0),
        ("position", "INTEGER", 1, None, 0),
    ),
    "observed_snapshots": (
        ("path_key", "TEXT", 0, None, 1),
        ("epoch", "INTEGER", 1, None, 0),
        ("snapshot_json", "TEXT", 1, None, 0),
        ("artifact_digest", "TEXT", 1, None, 0),
        ("artifact_size", "INTEGER", 1, None, 0),
        ("metadata_digest", "TEXT", 1, None, 0),
        ("metadata_size", "INTEGER", 1, None, 0),
        ("observed_at_ns", "INTEGER", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
    ),
}

_CREATE_STATEMENTS = (
    """
    CREATE TABLE timeline (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        pin_revision INTEGER NOT NULL CHECK (pin_revision >= 1),
        updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns >= 0)
    )
    """,
    """
    CREATE TABLE timeline_transitions (
        epoch INTEGER PRIMARY KEY CHECK (epoch >= 0),
        prior_epoch INTEGER NOT NULL CHECK (prior_epoch >= -1),
        occurred_at_ns INTEGER NOT NULL CHECK (occurred_at_ns >= 0),
        CHECK (epoch = 0 AND prior_epoch = -1 OR epoch > prior_epoch)
    )
    """,
    """
    CREATE TABLE cursor_leases (
        lease_id TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        root_digest TEXT NOT NULL,
        root_size INTEGER NOT NULL CHECK (root_size >= 0),
        secret BLOB NOT NULL CHECK (length(secret) = 32),
        issued_at_ns INTEGER NOT NULL CHECK (issued_at_ns >= 0),
        expires_at_ns INTEGER NOT NULL CHECK (expires_at_ns >= issued_at_ns),
        hard_expires_at_ns INTEGER NOT NULL CHECK (hard_expires_at_ns >= expires_at_ns),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        released_at_ns INTEGER CHECK (released_at_ns IS NULL OR released_at_ns >= issued_at_ns)
    )
    """,
    """
    CREATE TABLE cursor_pins (
        lease_id TEXT NOT NULL REFERENCES cursor_leases(lease_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        digest TEXT NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        PRIMARY KEY (lease_id, ordinal),
        UNIQUE (lease_id, digest)
    )
    """,
    """
    CREATE TABLE cursor_grants (
        grant_id TEXT PRIMARY KEY,
        lease_id TEXT NOT NULL REFERENCES cursor_leases(lease_id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position >= 0),
        UNIQUE (lease_id, position)
    )
    """,
    """
    CREATE TABLE observed_snapshots (
        path_key TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        snapshot_json TEXT NOT NULL,
        artifact_digest TEXT NOT NULL CHECK (length(artifact_digest) = 64),
        artifact_size INTEGER NOT NULL CHECK (artifact_size >= 0),
        metadata_digest TEXT NOT NULL CHECK (length(metadata_digest) = 64),
        metadata_size INTEGER NOT NULL CHECK (metadata_size >= 0),
        observed_at_ns INTEGER NOT NULL CHECK (observed_at_ns >= 0),
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )
    """,
)


@dataclass(frozen=True)
class CursorTimeline:
    epoch: int
    revision: int
    updated_at_ns: int


@dataclass(frozen=True)
class CursorLease:
    lease_id: str
    namespace: str
    epoch: int
    root_manifest: ContentIdentity
    pinned_artifacts: tuple[ContentIdentity, ...]
    issued_at_ns: int
    expires_at_ns: int
    hard_expires_at_ns: int
    revision: int


@dataclass(frozen=True)
class OpenCursor:
    lease: CursorLease
    position: int


@dataclass(frozen=True)
class CursorRegistryHealth:
    timeline: CursorTimeline
    active_leases: int
    expired_leases: int
    pinned_artifacts: int
    pinned_bytes: int
    nearest_expiry_ns: int | None
    observed_snapshots: int


@dataclass(frozen=True)
class ArtifactPinSnapshot:
    epoch: int
    revision: int
    artifacts: tuple[ContentIdentity, ...]


class DurableCursorRegistry:
    """SQLite authority for cursor capabilities, leases, and timeline epochs."""

    def __init__(
        self,
        path: Path,
        *,
        idle_ttl_ns: int = DEFAULT_CURSOR_IDLE_TTL_NS,
        hard_ttl_ns: int = DEFAULT_CURSOR_HARD_TTL_NS,
        now_ns: Callable[[], int] = time.time_ns,
        timeout: float = 20.0,
    ) -> None:
        self.path = Path(path)
        self.idle_ttl_ns = self._positive_integer(idle_ttl_ns, "idle TTL")
        self.hard_ttl_ns = self._positive_integer(hard_ttl_ns, "hard TTL")
        if self.idle_ttl_ns > self.hard_ttl_ns:
            raise ValueError("cursor idle TTL cannot exceed its hard TTL")
        if not callable(now_ns):
            raise TypeError("cursor clock must be callable")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("cursor registry timeout must be positive")
        self._now_ns = now_ns
        self._timeout = float(timeout)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    @property
    def current_epoch(self) -> int:
        return self.timeline().epoch

    def timeline(self) -> CursorTimeline:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT epoch, revision, updated_at_ns FROM timeline WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ReadCursorError("cursor timeline is missing")
        return CursorTimeline(epoch=row[0], revision=row[1], updated_at_ns=row[2])

    def health(self) -> CursorRegistryHealth:
        now = self._clock()
        with closing(self._connect()) as connection:
            timeline = self._timeline_row(connection)
            rows = connection.execute("""
                SELECT lease_id, epoch, expires_at_ns, hard_expires_at_ns,
                       released_at_ns
                FROM cursor_leases
                """).fetchall()
            active_ids = tuple(
                row[0] for row in rows if row[1] == timeline[0] and row[2] > now and row[3] > now and row[4] is None
            )
            expired = sum(1 for row in rows if row[4] is None and row[0] not in active_ids)
            pins: dict[str, int] = {}
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                for digest, size in connection.execute(
                    f"SELECT digest, size FROM cursor_pins WHERE lease_id IN ({placeholders})",
                    active_ids,
                ):
                    prior = pins.setdefault(digest, size)
                    if prior != size:
                        raise ReadCursorError("cursor pin digest resolves to conflicting sizes")
            observed_count = 0
            for artifact_digest, artifact_size, metadata_digest, metadata_size in connection.execute(
                """
                SELECT artifact_digest, artifact_size, metadata_digest, metadata_size
                FROM observed_snapshots WHERE epoch = ?
                """,
                (timeline[0],),
            ):
                observed_count += 1
                for digest, size in (
                    (artifact_digest, artifact_size),
                    (metadata_digest, metadata_size),
                ):
                    prior = pins.setdefault(digest, size)
                    if prior != size:
                        raise ReadCursorError("observed artifact digest resolves to conflicting sizes")
            nearest = min(
                (min(row[2], row[3]) for row in rows if row[0] in active_ids),
                default=None,
            )
        return CursorRegistryHealth(
            timeline=CursorTimeline(timeline[0], timeline[1], timeline[3]),
            active_leases=len(active_ids),
            expired_leases=expired,
            pinned_artifacts=len(pins),
            pinned_bytes=sum(pins.values()),
            nearest_expiry_ns=nearest,
            observed_snapshots=observed_count,
        )

    def pin_snapshot(self) -> ArtifactPinSnapshot:
        now = self._clock()
        with closing(self._connect()) as connection:
            return self._pin_snapshot(connection, now)

    @contextmanager
    def freeze_pins(self) -> Iterator[ArtifactPinSnapshot]:
        now = self._clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield self._pin_snapshot(connection, now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def observe(self, snapshot: FileSnapshot, *, expected_epoch: int) -> None:
        if type(snapshot) is not FileSnapshot:
            raise TypeError("observed snapshot is invalid")
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        self._validate_ref(snapshot.artifact)
        self._validate_ref(snapshot.metadata)
        path_key = self._path_key(snapshot.requested_path)
        snapshot_json = json.dumps(
            snapshot_to_dict(snapshot),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(snapshot_json.encode("ascii")) > _MAX_OBSERVED_SNAPSHOT_BYTES:
            raise ReadCursorError(
                "observed snapshot record exceeds the registry limit",
                maximum=_MAX_OBSERVED_SNAPSHOT_BYTES,
            )
        now = self._clock()
        with self._write_transaction() as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "observed snapshot belongs to a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            connection.execute(
                """
                INSERT INTO observed_snapshots (
                    path_key, epoch, snapshot_json, artifact_digest,
                    artifact_size, metadata_digest, metadata_size,
                    observed_at_ns, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(path_key) DO UPDATE SET
                    epoch = excluded.epoch,
                    snapshot_json = excluded.snapshot_json,
                    artifact_digest = excluded.artifact_digest,
                    artifact_size = excluded.artifact_size,
                    metadata_digest = excluded.metadata_digest,
                    metadata_size = excluded.metadata_size,
                    observed_at_ns = excluded.observed_at_ns,
                    revision = observed_snapshots.revision + 1
                """,
                (
                    path_key,
                    epoch,
                    snapshot_json,
                    snapshot.artifact.digest,
                    snapshot.artifact.size,
                    snapshot.metadata.digest,
                    snapshot.metadata.size,
                    now,
                ),
            )
            self._bump_pin_revision(connection)

    def observed(self, path: PathToken, *, expected_epoch: int) -> FileSnapshot | None:
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        path_key = self._path_key(path)
        with closing(self._connect()) as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "observed snapshot query uses a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            row = connection.execute(
                """
                SELECT snapshot_json FROM observed_snapshots
                WHERE path_key = ? AND epoch = ?
                """,
                (path_key, epoch),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            snapshot = snapshot_from_dict(payload)
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReadCursorError(
                "observed snapshot registry record is invalid",
                cause=exc,
            ) from exc
        if self._path_key(snapshot.requested_path) != path_key:
            raise ReadCursorError("observed snapshot path key is inconsistent")
        return snapshot

    def observations(self, *, expected_epoch: int) -> tuple[FileSnapshot, ...]:
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        with closing(self._connect()) as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "observed snapshot scan uses a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            rows = connection.execute(
                """
                SELECT path_key, snapshot_json FROM observed_snapshots
                WHERE epoch = ? ORDER BY path_key
                """,
                (epoch,),
            ).fetchall()
        snapshots = []
        for path_key, snapshot_json in rows:
            try:
                snapshot = snapshot_from_dict(json.loads(snapshot_json))
            except (
                TypeError,
                ValueError,
                UnicodeError,
                json.JSONDecodeError,
            ) as exc:
                raise ReadCursorError(
                    "observed snapshot registry record is invalid",
                    cause=exc,
                ) from exc
            if self._path_key(snapshot.requested_path) != path_key:
                raise ReadCursorError("observed snapshot path key is inconsistent")
            snapshots.append(snapshot)
        return tuple(snapshots)

    def forget(self, path: PathToken, *, expected_epoch: int) -> None:
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        path_key = self._path_key(path)
        with self._write_transaction() as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "observed snapshot deletion uses a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            cursor = connection.execute(
                "DELETE FROM observed_snapshots WHERE path_key = ? AND epoch = ?",
                (path_key, epoch),
            )
            if cursor.rowcount:
                self._bump_pin_revision(connection)

    def forget_all(self, *, expected_epoch: int) -> None:
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        with self._write_transaction() as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "observed snapshot deletion uses a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            cursor = connection.execute(
                "DELETE FROM observed_snapshots WHERE epoch = ?",
                (epoch,),
            )
            if cursor.rowcount:
                self._bump_pin_revision(connection)

    def issue(
        self,
        *,
        namespace: str,
        root_manifest: ContentIdentity,
        pinned_artifacts: Iterable[ContentIdentity],
        position: int,
        expected_epoch: int,
    ) -> str:
        namespace = self._validate_namespace(namespace)
        root_manifest = self._validate_ref(root_manifest)
        position = self._nonnegative_integer(position, "cursor position")
        expected_epoch = self._nonnegative_integer(expected_epoch, "expected epoch")
        pins = self._canonical_pins(root_manifest, pinned_artifacts)
        now = self._clock()
        hard_expiry = self._bounded_add(now, self.hard_ttl_ns, "hard TTL")
        idle_expiry = min(
            self._bounded_add(now, self.idle_ttl_ns, "idle TTL"),
            hard_expiry,
        )
        lease_id = uuid.uuid4().hex
        secret = os.urandom(_TOKEN_BYTES)
        token = self._grant_token(secret, namespace, position)
        grant_id = self._grant_id(token)
        with self._write_transaction() as connection:
            epoch = self._timeline_row(connection)[0]
            if epoch != expected_epoch:
                raise ReadCursorError(
                    "cursor source belongs to a stale timeline epoch",
                    expected_epoch=expected_epoch,
                    actual_epoch=epoch,
                )
            connection.execute(
                """
                INSERT INTO cursor_leases (
                    lease_id, namespace, epoch, root_digest, root_size, secret,
                    issued_at_ns, expires_at_ns, hard_expires_at_ns, revision,
                    released_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
                """,
                (
                    lease_id,
                    namespace,
                    epoch,
                    root_manifest.digest,
                    root_manifest.size,
                    secret,
                    now,
                    idle_expiry,
                    hard_expiry,
                ),
            )
            connection.executemany(
                "INSERT INTO cursor_pins (lease_id, ordinal, digest, size) VALUES (?, ?, ?, ?)",
                ((lease_id, ordinal, ref.digest, ref.size) for ordinal, ref in enumerate(pins)),
            )
            connection.execute(
                "INSERT INTO cursor_grants (grant_id, lease_id, position) VALUES (?, ?, ?)",
                (grant_id, lease_id, position),
            )
            self._bump_pin_revision(connection)
        return token

    def open(self, token: str, *, expected_namespace: str) -> OpenCursor:
        expected_namespace = self._validate_namespace(expected_namespace)
        grant_id = self._grant_id(token)
        now = self._clock()
        with self._write_transaction() as connection:
            row = self._open_row(
                connection,
                grant_id=grant_id,
                expected_namespace=expected_namespace,
                now=now,
            )
            row = self._renew(connection, row, now)
            return self._open_cursor(connection, row)

    def advance(
        self,
        token: str,
        *,
        expected_namespace: str,
        position: int,
    ) -> str:
        expected_namespace = self._validate_namespace(expected_namespace)
        position = self._nonnegative_integer(position, "cursor position")
        grant_id = self._grant_id(token)
        now = self._clock()
        with self._write_transaction() as connection:
            row = self._open_row(
                connection,
                grant_id=grant_id,
                expected_namespace=expected_namespace,
                now=now,
            )
            row = self._renew(connection, row, now)
            next_token = self._grant_token(row[6], row[1], position)
            next_grant_id = self._grant_id(next_token)
            existing = connection.execute(
                "SELECT lease_id, position FROM cursor_grants WHERE grant_id = ?",
                (next_grant_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO cursor_grants (grant_id, lease_id, position) VALUES (?, ?, ?)",
                    (next_grant_id, row[0], position),
                )
            elif existing != (row[0], position):
                raise ReadCursorError("cursor grant identity collision")
        return next_token

    def release(self, token: str, *, expected_namespace: str) -> None:
        expected_namespace = self._validate_namespace(expected_namespace)
        grant_id = self._grant_id(token)
        now = self._clock()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT l.lease_id, l.namespace, l.released_at_ns
                FROM cursor_grants AS g
                JOIN cursor_leases AS l ON l.lease_id = g.lease_id
                WHERE g.grant_id = ?
                """,
                (grant_id,),
            ).fetchone()
            if row is None:
                raise ReadCursorError("cursor token is invalid")
            if row[1] != expected_namespace:
                raise ReadCursorError("cursor belongs to a different namespace")
            if row[2] is None:
                cursor = connection.execute(
                    """
                    UPDATE cursor_leases
                    SET released_at_ns = ?, revision = revision + 1
                    WHERE lease_id = ? AND released_at_ns IS NULL
                    """,
                    (now, row[0]),
                )
                if cursor.rowcount:
                    self._bump_pin_revision(connection)

    def invalidate(self) -> CursorTimeline:
        now = self._clock()
        with self._write_transaction() as connection:
            epoch, revision, _, _ = self._timeline_row(connection)
            return self._set_epoch(
                connection,
                prior_epoch=epoch,
                epoch=epoch + 1,
                revision=revision + 1,
                now=now,
            )

    def invalidate_observation(self, path: PathToken) -> CursorTimeline:
        """Atomically stale every cursor and forget one observed path.

        Unaffected observed snapshots advance to the new epoch in the same
        transaction, so an external change cannot erase unrelated read facts.
        """
        path_key = self._path_key(path)
        now = self._clock()
        with self._write_transaction() as connection:
            epoch, revision, _, _ = self._timeline_row(connection)
            return self._set_epoch(
                connection,
                prior_epoch=epoch,
                epoch=epoch + 1,
                revision=revision + 1,
                now=now,
                invalidated_path_key=path_key,
            )

    def synchronize(self, minimum_epoch: int) -> CursorTimeline:
        minimum_epoch = self._nonnegative_integer(minimum_epoch, "minimum epoch")
        now = self._clock()
        with self._write_transaction() as connection:
            epoch, revision, _, updated_at = self._timeline_row(connection)
            if epoch >= minimum_epoch:
                return CursorTimeline(epoch, revision, updated_at)
            return self._set_epoch(
                connection,
                prior_epoch=epoch,
                epoch=minimum_epoch,
                revision=revision + 1,
                now=now,
            )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = self._table_names(connection)
                if version == 0 and not tables:
                    for statement in _CREATE_STATEMENTS:
                        connection.execute(statement)
                    now = self._clock()
                    connection.execute(
                        "INSERT INTO timeline VALUES (1, 0, 1, 1, ?)",
                        (now,),
                    )
                    connection.execute(
                        "INSERT INTO timeline_transitions VALUES (0, -1, ?)",
                        (now,),
                    )
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                elif version != _SCHEMA_VERSION:
                    raise ReadCursorError(
                        "cursor registry schema requires an explicit migration",
                        expected_schema=_SCHEMA_VERSION,
                        actual_schema=version,
                    )
                self._validate_schema(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fsync_parent()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._timeout,
                isolation_level=None,
            )
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1_000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise ReadCursorError("cannot open durable cursor registry", cause=exc) from exc

    def _write_transaction(self):
        return _ImmediateTransaction(self._connect())

    def _pin_snapshot(
        self,
        connection: sqlite3.Connection,
        now: int,
    ) -> ArtifactPinSnapshot:
        epoch, _, pin_revision, _ = self._timeline_row(connection)
        refs = [
            ContentIdentity(digest=digest, size=size)
            for digest, size in connection.execute(
                """
                SELECT p.digest, p.size
                FROM cursor_pins AS p
                JOIN cursor_leases AS l ON l.lease_id = p.lease_id
                WHERE l.epoch = ? AND l.released_at_ns IS NULL
                  AND l.expires_at_ns > ? AND l.hard_expires_at_ns > ?
                """,
                (epoch, now, now),
            )
        ]
        for artifact_digest, artifact_size, metadata_digest, metadata_size in connection.execute(
            """
            SELECT artifact_digest, artifact_size, metadata_digest, metadata_size
            FROM observed_snapshots WHERE epoch = ?
            """,
            (epoch,),
        ):
            refs.extend(
                (
                    ContentIdentity(artifact_digest, artifact_size),
                    ContentIdentity(metadata_digest, metadata_size),
                )
            )
        return ArtifactPinSnapshot(
            epoch=epoch,
            revision=pin_revision,
            artifacts=self._canonical_refs(refs),
        )

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        tables = self._table_names(connection)
        if tables != frozenset(_TABLE_COLUMNS):
            raise ReadCursorError("cursor registry tables are not canonical")
        for table, expected in _TABLE_COLUMNS.items():
            actual = tuple(
                (row[1], row[2], row[3], row[4], row[5]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise ReadCursorError(
                    "cursor registry columns are not canonical",
                    table=table,
                )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ReadCursorError("cursor registry foreign keys are corrupt")
        if connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] != 1:
            raise ReadCursorError("cursor registry timeline is not canonical")

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )

    @staticmethod
    def _timeline_row(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
        row = connection.execute(
            "SELECT epoch, revision, pin_revision, updated_at_ns " "FROM timeline WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ReadCursorError("cursor timeline is missing")
        return row

    def _open_row(
        self,
        connection: sqlite3.Connection,
        *,
        grant_id: str,
        expected_namespace: str,
        now: int,
    ) -> tuple:
        row = connection.execute(
            """
            SELECT l.lease_id, l.namespace, l.epoch, l.root_digest, l.root_size,
                   g.position, l.secret, l.issued_at_ns, l.expires_at_ns,
                   l.hard_expires_at_ns, l.revision, l.released_at_ns
            FROM cursor_grants AS g
            JOIN cursor_leases AS l ON l.lease_id = g.lease_id
            WHERE g.grant_id = ?
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            raise ReadCursorError("cursor token is invalid")
        if row[1] != expected_namespace:
            raise ReadCursorError("cursor belongs to a different namespace")
        if row[11] is not None:
            raise ReadCursorError("cursor lease was released")
        if row[2] != self._timeline_row(connection)[0]:
            raise ReadCursorError("cursor belongs to a stale timeline epoch")
        if now >= row[8] or now >= row[9]:
            raise ReadCursorError("cursor lease expired")
        return row

    def _renew(self, connection: sqlite3.Connection, row: tuple, now: int) -> tuple:
        renewed = min(self._bounded_add(now, self.idle_ttl_ns, "idle TTL"), row[9])
        if renewed <= row[8]:
            return row
        cursor = connection.execute(
            """
            UPDATE cursor_leases
            SET expires_at_ns = ?, revision = revision + 1
            WHERE lease_id = ? AND revision = ? AND released_at_ns IS NULL
            """,
            (renewed, row[0], row[10]),
        )
        if cursor.rowcount != 1:
            raise ReadCursorError("cursor lease changed concurrently")
        return row[:8] + (renewed, row[9], row[10] + 1, row[11])

    @staticmethod
    def _open_cursor(connection: sqlite3.Connection, row: tuple) -> OpenCursor:
        pins = tuple(
            ContentIdentity(digest=digest, size=size)
            for digest, size in connection.execute(
                """
                SELECT digest, size FROM cursor_pins
                WHERE lease_id = ? ORDER BY ordinal
                """,
                (row[0],),
            )
        )
        return OpenCursor(
            lease=CursorLease(
                lease_id=row[0],
                namespace=row[1],
                epoch=row[2],
                root_manifest=ContentIdentity(digest=row[3], size=row[4]),
                pinned_artifacts=pins,
                issued_at_ns=row[7],
                expires_at_ns=row[8],
                hard_expires_at_ns=row[9],
                revision=row[10],
            ),
            position=row[5],
        )

    @staticmethod
    def _set_epoch(
        connection: sqlite3.Connection,
        *,
        prior_epoch: int,
        epoch: int,
        revision: int,
        now: int,
        invalidated_path_key: str | None = None,
    ) -> CursorTimeline:
        if epoch > _MAX_INTEGER:
            raise ReadCursorError("cursor timeline epoch is exhausted")
        connection.execute(
            "INSERT INTO timeline_transitions VALUES (?, ?, ?)",
            (epoch, prior_epoch, now),
        )
        cursor = connection.execute(
            """
            UPDATE timeline SET epoch = ?, revision = ?, updated_at_ns = ?
            WHERE singleton = 1 AND epoch = ?
            """,
            (epoch, revision, now, prior_epoch),
        )
        if cursor.rowcount != 1:
            raise ReadCursorError("cursor timeline changed concurrently")
        if invalidated_path_key is None:
            connection.execute("DELETE FROM observed_snapshots")
        else:
            connection.execute(
                "DELETE FROM observed_snapshots WHERE path_key = ?",
                (invalidated_path_key,),
            )
            connection.execute(
                "UPDATE observed_snapshots SET epoch = ?",
                (epoch,),
            )
        DurableCursorRegistry._bump_pin_revision(connection)
        return CursorTimeline(epoch, revision, now)

    @staticmethod
    def _bump_pin_revision(connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            """
            UPDATE timeline SET pin_revision = pin_revision + 1
            WHERE singleton = 1 AND pin_revision < ?
            """,
            (_MAX_INTEGER,),
        )
        if cursor.rowcount != 1:
            raise ReadCursorError("cursor pin revision is exhausted")

    @staticmethod
    def _path_key(path: PathToken) -> str:
        if type(path) is not PathToken:
            raise TypeError("observed snapshot path is invalid")
        return json.dumps(
            path_to_dict(path),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _canonical_pins(
        cls,
        root: ContentIdentity,
        refs: Iterable[ContentIdentity],
    ) -> tuple[ContentIdentity, ...]:
        try:
            values = {root, *(cls._validate_ref(ref) for ref in refs)}
        except TypeError as exc:
            raise ReadCursorError("cursor pinned artifacts are invalid", cause=exc) from exc
        sizes_by_digest: dict[str, int] = {}
        for ref in values:
            prior = sizes_by_digest.setdefault(ref.digest, ref.size)
            if prior != ref.size:
                raise ReadCursorError(
                    "cursor pinned artifact digest has conflicting sizes",
                    digest=ref.digest,
                )
        if len(values) > _MAX_PINNED_ARTIFACTS:
            raise ReadCursorError(
                "cursor lease pins too many artifacts",
                maximum=_MAX_PINNED_ARTIFACTS,
            )
        return tuple(sorted(values, key=lambda ref: (ref.digest, ref.size)))

    @classmethod
    def _canonical_refs(cls, refs: Iterable[ContentIdentity]) -> tuple[ContentIdentity, ...]:
        sizes: dict[str, int] = {}
        for ref in refs:
            validated = cls._validate_ref(ref)
            prior = sizes.setdefault(validated.digest, validated.size)
            if prior != validated.size:
                raise ReadCursorError(
                    "pinned artifact digest resolves to conflicting sizes",
                    digest=validated.digest,
                )
        return tuple(ContentIdentity(ContentDigest(digest), size) for digest, size in sorted(sizes.items()))

    @staticmethod
    def _validate_ref(ref: ContentIdentity) -> ContentIdentity:
        if (
            type(ref) is not ContentIdentity
            or not isinstance(ref.digest, str)
            or len(ref.digest) != 64
            or any(character not in _DIGEST_CHARACTERS for character in ref.digest)
            or type(ref.size) is not int
            or not 0 <= ref.size <= _MAX_INTEGER
        ):
            raise ReadCursorError("cursor artifact reference is invalid")
        return ref

    @staticmethod
    def _validate_namespace(namespace: str) -> str:
        if type(namespace) is not str or not namespace:
            raise ReadCursorError("cursor namespace is invalid")
        try:
            encoded = namespace.encode("ascii", errors="strict")
        except UnicodeError as exc:
            raise ReadCursorError("cursor namespace is invalid", cause=exc) from exc
        if len(encoded) > _MAX_NAMESPACE_BYTES:
            raise ReadCursorError("cursor namespace is too long")
        return namespace

    @staticmethod
    def _grant_token(secret: bytes, namespace: str, position: int) -> str:
        payload = namespace.encode("ascii") + b"\0" + position.to_bytes(8, "big")
        raw = hmac.digest(secret, payload, "sha256")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _grant_id(token: str) -> str:
        if type(token) is not str or len(token) != _TOKEN_CHARACTERS:
            raise ReadCursorError("cursor token is invalid")
        try:
            token.encode("ascii", errors="strict")
            raw = base64.b64decode(token + "=", altchars=b"-_", validate=True)
        except (UnicodeError, ValueError) as exc:
            raise ReadCursorError("cursor token is invalid", cause=exc) from exc
        if len(raw) != _TOKEN_BYTES or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token:
            raise ReadCursorError("cursor token is invalid")
        return hashlib.sha256(raw).hexdigest()

    def _clock(self) -> int:
        value = self._now_ns()
        if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
            raise ReadCursorError("cursor clock returned an invalid timestamp")
        return value

    @staticmethod
    def _bounded_add(value: int, increment: int, field: str) -> int:
        result = value + increment
        if result > _MAX_INTEGER:
            raise ReadCursorError(f"cursor {field} exceeds the durable integer range")
        return result

    @staticmethod
    def _positive_integer(value: int, field: str) -> int:
        if type(value) is not int or not 0 < value <= _MAX_INTEGER:
            raise ValueError(f"cursor {field} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_integer(value: int, field: str) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
            raise ReadCursorError(f"{field} is invalid")
        return value

    def _fsync_parent(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _ImmediateTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection
        except Exception:
            self.connection.close()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


__all__ = [
    "ArtifactPinSnapshot",
    "CursorLease",
    "CursorRegistryHealth",
    "CursorTimeline",
    "DEFAULT_CURSOR_HARD_TTL_NS",
    "DEFAULT_CURSOR_IDLE_TTL_NS",
    "DurableCursorRegistry",
    "OpenCursor",
]
