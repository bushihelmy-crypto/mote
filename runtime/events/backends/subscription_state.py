"""SQLite durability for subscription checkpoints and dead letters."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from mote.contracts.events.envelope import StreamId
from mote.contracts.ports.events.subscription import (
    DeadLetterEntry,
    SubscriptionCheckpoint,
    SubscriptionIdentity,
    SubscriptionOwnerLease,
)
from mote.runtime.events.journal import decode_event_record, encode_event_record
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.telemetry.logging import log_class

_FORMAT_VERSION = 2
_MAX_DEAD_LETTER_PAGE = 1_000


class SubscriptionStateIntegrityError(RuntimeError):
    """Persisted subscription state violates its monotonic identity contract."""


class CheckpointRegressionError(SubscriptionStateIntegrityError):
    """A stale owner attempted to move a subscription checkpoint backwards."""


class SubscriptionStateStoreClosed(RuntimeError):
    """The state store is not open for operations."""


@log_class(level="DEBUG", exclude={"path"})
class SQLiteSubscriptionStateStore:
    """One explicitly opened local state authority shared by fabric workers."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    async def aopen(self) -> None:
        await run_disk_io(self._open_sync)

    async def aclose(self) -> None:
        await run_disk_io(self._close_sync)

    async def load(
        self,
        identity: SubscriptionIdentity,
        stream_id: StreamId,
    ) -> int:
        return await run_disk_io(self._load_sync, identity, stream_id)

    async def claim_owner(self, identity: SubscriptionIdentity, owner_id: str) -> SubscriptionOwnerLease:
        return await run_disk_io(self._claim_owner_sync, identity, owner_id)

    def _claim_owner_sync(self, identity: SubscriptionIdentity, owner_id: str) -> SubscriptionOwnerLease:
        if type(owner_id) is not str or not owner_id:
            raise ValueError("subscription owner identity is invalid")
        with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation, fencing_token FROM subscription_owners WHERE subscription = ?",
                (str(identity),),
            ).fetchone()
            generation = 1 if row is None else row["generation"] + 1
            fence = 1 if row is None else row["fencing_token"] + 1
            connection.execute(
                "INSERT INTO subscription_owners(subscription, owner_id, generation, fencing_token) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(subscription) DO UPDATE SET owner_id=excluded.owner_id, generation=excluded.generation, "
                "fencing_token=excluded.fencing_token",
                (str(identity), owner_id, generation, fence),
            )
            connection.commit()
            return SubscriptionOwnerLease(identity, owner_id, generation, fence)

    async def save(self, checkpoint: SubscriptionCheckpoint) -> None:
        await run_disk_io(self._save_sync, checkpoint)

    async def quarantine(
        self,
        entry: DeadLetterEntry,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        if (
            entry.subscription != checkpoint.identity
            or entry.stream_id != checkpoint.stream_id
            or entry.sequence != checkpoint.sequence
        ):
            raise ValueError("dead letter and checkpoint identify different delivery")
        await run_disk_io(self._quarantine_sync, entry, checkpoint)

    async def list_dead_letters(
        self,
        *,
        subscription: SubscriptionIdentity | None = None,
        limit: int = 100,
    ) -> tuple[DeadLetterEntry, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_DEAD_LETTER_PAGE:
            raise ValueError("dead-letter page size is outside its bound")
        return await run_disk_io(
            self._list_dead_letters_sync,
            subscription,
            limit,
        )

    async def prune_dead_letters(
        self,
        *,
        content_before: datetime,
        tombstone_before: datetime,
        limit: int = 1_000,
    ) -> tuple[int, int]:
        if content_before.tzinfo is None or tombstone_before.tzinfo is None or tombstone_before >= content_before:
            raise ValueError("dead-letter retention boundaries are invalid")
        if type(limit) is not int or not 1 <= limit <= _MAX_DEAD_LETTER_PAGE:
            raise ValueError("dead-letter retention scan is outside its bound")
        return await run_disk_io(self._prune_dead_letters_sync, content_before, tombstone_before, limit)

    def _prune_dead_letters_sync(
        self, content_before: datetime, tombstone_before: datetime, limit: int
    ) -> tuple[int, int]:
        with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                "SELECT subscription, stream_id, sequence, event_id, last_failed_at FROM subscription_dead_letters "
                "WHERE last_failed_at < ? ORDER BY last_failed_at LIMIT ?",
                (content_before.isoformat(), limit),
            ).fetchall()
            for row in expired:
                connection.execute(
                    "INSERT OR IGNORE INTO subscription_dead_letter_tombstones "
                    "(subscription, stream_id, sequence, event_id, retired_at) VALUES (?, ?, ?, ?, ?)",
                    (row["subscription"], row["stream_id"], row["sequence"], row["event_id"], row["last_failed_at"]),
                )
                connection.execute(
                    "DELETE FROM subscription_dead_letters WHERE subscription=? AND stream_id=? AND sequence=?",
                    (row["subscription"], row["stream_id"], row["sequence"]),
                )
            tombstones = connection.execute(
                "SELECT subscription, stream_id, sequence FROM subscription_dead_letter_tombstones "
                "WHERE retired_at < ? ORDER BY retired_at LIMIT ?",
                (tombstone_before.isoformat(), limit),
            ).fetchall()
            for row in tombstones:
                connection.execute(
                    "DELETE FROM subscription_dead_letter_tombstones WHERE subscription=? AND stream_id=? AND sequence=?",
                    (row["subscription"], row["stream_id"], row["sequence"]),
                )
            connection.commit()
            return len(expired), len(tombstones)

    def _open_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self._path,
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA busy_timeout = 30000")
                self._initialize(connection)
            except Exception:
                if connection is not None:
                    connection.close()
                raise
            assert connection is not None
            self._connection = connection

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def _load_sync(
        self,
        identity: SubscriptionIdentity,
        stream_id: StreamId,
    ) -> int:
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT sequence FROM subscription_checkpoints
                WHERE subscription = ? AND stream_id = ?
                """,
                (str(identity), str(stream_id)),
            ).fetchone()
            return 0 if row is None else int(row["sequence"])

    def _save_sync(self, checkpoint: SubscriptionCheckpoint) -> None:
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._save_in_transaction(connection, checkpoint)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _quarantine_sync(
        self,
        entry: DeadLetterEntry,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        envelope_record, _ = encode_event_record(entry.envelope, None)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT event_id FROM subscription_dead_letters
                    WHERE subscription = ? AND stream_id = ? AND sequence = ?
                    """,
                    (
                        str(entry.subscription),
                        str(entry.stream_id),
                        entry.sequence,
                    ),
                ).fetchone()
                if existing is not None and existing["event_id"] != entry.event_id:
                    raise SubscriptionStateIntegrityError("one dead-letter position contains different event IDs")
                connection.execute(
                    """
                    INSERT INTO subscription_dead_letters (
                        subscription, stream_id, sequence, event_id,
                        envelope_record, attempts, error,
                        first_failed_at, last_failed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subscription, stream_id, sequence) DO UPDATE SET
                        envelope_record = excluded.envelope_record,
                        attempts = MAX(subscription_dead_letters.attempts, excluded.attempts),
                        error = excluded.error,
                        first_failed_at = MIN(
                            subscription_dead_letters.first_failed_at,
                            excluded.first_failed_at
                        ),
                        last_failed_at = MAX(
                            subscription_dead_letters.last_failed_at,
                            excluded.last_failed_at
                        )
                    """,
                    (
                        str(entry.subscription),
                        str(entry.stream_id),
                        entry.sequence,
                        str(entry.event_id),
                        envelope_record,
                        entry.attempts,
                        entry.error,
                        entry.first_failed_at.isoformat(),
                        entry.last_failed_at.isoformat(),
                    ),
                )
                self._save_in_transaction(connection, checkpoint)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _list_dead_letters_sync(
        self,
        subscription: SubscriptionIdentity | None,
        limit: int,
    ) -> tuple[DeadLetterEntry, ...]:
        with self._lock:
            connection = self._require_connection()
            if subscription is None:
                rows = connection.execute(
                    """
                    SELECT * FROM subscription_dead_letters
                    ORDER BY last_failed_at DESC, subscription, stream_id, sequence
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM subscription_dead_letters
                    WHERE subscription = ?
                    ORDER BY last_failed_at DESC, stream_id, sequence
                    LIMIT ?
                    """,
                    (str(subscription), limit),
                ).fetchall()
            return tuple(self._dead_letter_from_row(row) for row in rows)

    def _save_in_transaction(
        self,
        connection: sqlite3.Connection,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        row = connection.execute(
            """
            SELECT sequence, owner_id, generation, fencing_token FROM subscription_checkpoints
            WHERE subscription = ? AND stream_id = ?
            """,
            (str(checkpoint.identity), str(checkpoint.stream_id)),
        ).fetchone()
        if row is not None and int(row["sequence"]) > checkpoint.sequence:
            raise CheckpointRegressionError(
                f"checkpoint for {checkpoint.identity!r}/{checkpoint.stream_id!r} "
                f"would regress from {row['sequence']} to {checkpoint.sequence}"
            )
        owner = connection.execute(
            "SELECT owner_id, generation, fencing_token FROM subscription_owners WHERE subscription = ?",
            (str(checkpoint.identity),),
        ).fetchone()
        if owner is None or (owner["owner_id"], owner["generation"], owner["fencing_token"]) != (
            checkpoint.owner_id,
            checkpoint.generation,
            checkpoint.fencing_token,
        ):
            raise CheckpointRegressionError("stale subscription owner cannot advance checkpoint")
        connection.execute(
            """
            INSERT INTO subscription_checkpoints (
                subscription, stream_id, sequence, owner_id, generation, fencing_token, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subscription, stream_id) DO UPDATE SET
                sequence = excluded.sequence,
                owner_id = excluded.owner_id,
                generation = excluded.generation,
                fencing_token = excluded.fencing_token,
                updated_at = excluded.updated_at
            """,
            (
                str(checkpoint.identity),
                str(checkpoint.stream_id),
                checkpoint.sequence,
                checkpoint.owner_id,
                checkpoint.generation,
                checkpoint.fencing_token,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    @staticmethod
    def _dead_letter_from_row(row: sqlite3.Row) -> DeadLetterEntry:
        envelope = decode_event_record(bytes(row["envelope_record"]))
        if (
            envelope.event_id != row["event_id"]
            or envelope.stream_id != row["stream_id"]
            or envelope.sequence != row["sequence"]
        ):
            raise SubscriptionStateIntegrityError("dead-letter envelope does not match its indexed identity")
        return DeadLetterEntry(
            subscription=SubscriptionIdentity(row["subscription"]),
            envelope=envelope,
            attempts=row["attempts"],
            error=row["error"],
            first_failed_at=datetime.fromisoformat(row["first_failed_at"]),
            last_failed_at=datetime.fromisoformat(row["last_failed_at"]),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SubscriptionStateStoreClosed("subscription state store is closed")
        return self._connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, _FORMAT_VERSION}:
            raise SubscriptionStateIntegrityError(f"subscription state format {version} is unsupported")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS subscription_checkpoints (
                    subscription TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(subscription, stream_id)
                ) WITHOUT ROWID
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS subscription_dead_letter_tombstones (
                    subscription TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    event_id TEXT NOT NULL,
                    retired_at TEXT NOT NULL,
                    PRIMARY KEY(subscription, stream_id, sequence)
                ) WITHOUT ROWID
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS subscription_owners (
                    subscription TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0)
                ) WITHOUT ROWID
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS subscription_dead_letters (
                    subscription TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    event_id TEXT NOT NULL,
                    envelope_record BLOB NOT NULL,
                    attempts INTEGER NOT NULL CHECK(attempts > 0),
                    error TEXT NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    PRIMARY KEY(subscription, stream_id, sequence)
                ) WITHOUT ROWID
                """)
            expected_columns = {
                "subscription_checkpoints": (
                    "subscription",
                    "stream_id",
                    "sequence",
                    "owner_id",
                    "generation",
                    "fencing_token",
                    "updated_at",
                ),
                "subscription_owners": ("subscription", "owner_id", "generation", "fencing_token"),
                "subscription_dead_letters": (
                    "subscription",
                    "stream_id",
                    "sequence",
                    "event_id",
                    "envelope_record",
                    "attempts",
                    "error",
                    "first_failed_at",
                    "last_failed_at",
                ),
                "subscription_dead_letter_tombstones": (
                    "subscription",
                    "stream_id",
                    "sequence",
                    "event_id",
                    "retired_at",
                ),
            }
            for table, expected in expected_columns.items():
                actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
                if actual != expected:
                    raise SubscriptionStateIntegrityError(f"subscription state table {table!r} has an invalid schema")
            if version == 0:
                connection.execute(f"PRAGMA user_version = {_FORMAT_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


__all__ = [
    "CheckpointRegressionError",
    "SQLiteSubscriptionStateStore",
    "SubscriptionStateIntegrityError",
    "SubscriptionStateStoreClosed",
]
