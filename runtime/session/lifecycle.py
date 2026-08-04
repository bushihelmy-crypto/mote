"""Canonical durable Session lifecycle, retention, and deletion authority."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from mote.contracts.session import (
    SESSION_STREAM_RETENTION_SECONDS,
    SessionBlockerKind,
    SessionDeletionClaim,
    SessionDeletionCommand,
    SessionDeletionReceipt,
    SessionDeletionState,
    SessionEligibilitySnapshot,
    SessionId,
    SessionLifecycleState,
)

SESSION_LIFECYCLE_SCHEMA = "mote.session-lifecycle/v2"


class SessionLifecycleConflictError(RuntimeError):
    pass


class SessionLifecycleStore:
    """SQLite truth for lifecycle generation, blockers, and deletion receipts."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._clock = clock

    def activate(self, session_id: SessionId) -> SessionEligibilitySnapshot:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO sessions(session_id, generation, revision, state, blockers, terminal_at, updated_at) "
                    "VALUES (?, 1, 1, ?, '[]', NULL, ?)",
                    (str(session_id), SessionLifecycleState.ACTIVE.value, self._now()),
                )
            elif row["state"] in {SessionLifecycleState.TOMBSTONED.value, SessionLifecycleState.DELETING.value}:
                raise SessionLifecycleConflictError("deleted Session identity cannot be reused")
            elif row["state"] != SessionLifecycleState.ACTIVE.value:
                connection.execute(
                    "UPDATE sessions SET generation = generation + 1, revision = revision + 1, state = ?, "
                    "terminal_at = NULL, updated_at = ? WHERE session_id = ?",
                    (SessionLifecycleState.ACTIVE.value, self._now(), str(session_id)),
                )
            connection.commit()
        return self.get(session_id)

    def set_state(
        self,
        session_id: SessionId,
        state: SessionLifecycleState,
        *,
        expected_generation: int,
        expected_revision: int,
    ) -> SessionEligibilitySnapshot:
        allowed = {
            SessionLifecycleState.ACTIVE: {SessionLifecycleState.DRAINING, SessionLifecycleState.RECOVERY},
            SessionLifecycleState.DRAINING: {SessionLifecycleState.RECOVERY, SessionLifecycleState.TERMINAL},
            SessionLifecycleState.RECOVERY: {SessionLifecycleState.ACTIVE, SessionLifecycleState.DRAINING},
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expected(connection, session_id, expected_generation, expected_revision)
            current = SessionLifecycleState(row["state"])
            if state not in allowed.get(current, set()):
                raise SessionLifecycleConflictError("Session lifecycle transition is invalid")
            blockers = self._decode_blockers(row["blockers"])
            if state is SessionLifecycleState.TERMINAL and blockers:
                raise SessionLifecycleConflictError("Session cannot become terminal with unsettled blockers")
            terminal_at = self._now() if state is SessionLifecycleState.TERMINAL else None
            connection.execute(
                "UPDATE sessions SET revision = revision + 1, state = ?, terminal_at = ?, updated_at = ? "
                "WHERE session_id = ? AND revision = ?",
                (state.value, terminal_at, self._now(), str(session_id), expected_revision),
            )
            connection.commit()
        return self.get(session_id)

    def replace_blockers(
        self,
        session_id: SessionId,
        blockers: tuple[SessionBlockerKind, ...],
        *,
        expected_generation: int,
        expected_revision: int,
    ) -> SessionEligibilitySnapshot:
        canonical = tuple(sorted(set(blockers), key=lambda item: item.value))
        if blockers != canonical:
            raise ValueError("Session blockers must be unique and sorted")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expected(connection, session_id, expected_generation, expected_revision)
            if (
                row["state"]
                in {
                    SessionLifecycleState.TERMINAL.value,
                    SessionLifecycleState.DELETING.value,
                    SessionLifecycleState.TOMBSTONED.value,
                }
                and blockers
            ):
                raise SessionLifecycleConflictError("closed Session cannot acquire blockers")
            connection.execute(
                "UPDATE sessions SET revision = revision + 1, blockers = ?, updated_at = ? "
                "WHERE session_id = ? AND revision = ?",
                (json.dumps(tuple(item.value for item in blockers)), self._now(), str(session_id), expected_revision),
            )
            connection.commit()
        return self.get(session_id)

    def get(self, session_id: SessionId) -> SessionEligibilitySnapshot:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)).fetchone()
            if row is None:
                raise KeyError(str(session_id))
            return self._snapshot(row)

    def scan_retention_eligible(
        self, *, before: datetime, after_session_id: str = "", limit: int = 128
    ) -> tuple[SessionEligibilitySnapshot, ...]:
        if before.tzinfo is None or limit < 1 or limit > 4096:
            raise ValueError("Session retention scan bounds are invalid")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE session_id > ? AND state = ? AND blockers = '[]' "
                "AND terminal_at <= ? ORDER BY session_id LIMIT ?",
                (
                    after_session_id,
                    SessionLifecycleState.TERMINAL.value,
                    before.astimezone(timezone.utc).isoformat(),
                    limit,
                ),
            ).fetchall()
            return tuple(self._snapshot(row) for row in rows)

    def claim_deletion(
        self, command: SessionDeletionCommand, *, owner_id: str, fencing_token: int
    ) -> SessionDeletionClaim:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expected(
                connection, command.session_id, command.expected_lifecycle_generation, command.expected_revision
            )
            if row["state"] != SessionLifecycleState.TERMINAL.value or row["blockers"] != "[]":
                raise SessionLifecycleConflictError("Session is not deletion eligible")
            terminal_at = datetime.fromisoformat(row["terminal_at"])
            if (
                command.requested_at.astimezone(timezone.utc) - terminal_at
            ).total_seconds() < SESSION_STREAM_RETENTION_SECONDS:
                raise SessionLifecycleConflictError("Session retention window has not elapsed")
            prior = connection.execute("SELECT * FROM deletions WHERE command_id = ?", (command.command_id,)).fetchone()
            if prior is not None and prior["session_id"] != str(command.session_id):
                raise SessionLifecycleConflictError("Session deletion command preimage conflicts")
            revision = 1 if prior is None else prior["revision"] + 1
            connection.execute(
                "INSERT INTO deletions(command_id, session_id, generation, revision, state, owner_id, "
                "fencing_token, requested_at, updated_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '') "
                "ON CONFLICT(command_id) DO UPDATE SET revision=excluded.revision, state=excluded.state, "
                "owner_id=excluded.owner_id, fencing_token=excluded.fencing_token, updated_at=excluded.updated_at",
                (
                    command.command_id,
                    str(command.session_id),
                    row["generation"],
                    revision,
                    SessionDeletionState.CLAIMED.value,
                    owner_id,
                    fencing_token,
                    command.requested_at.astimezone(timezone.utc).isoformat(),
                    self._now(),
                ),
            )
            connection.execute(
                "UPDATE sessions SET revision = revision + 1, state = ?, updated_at = ? WHERE session_id = ?",
                (SessionLifecycleState.DELETING.value, self._now(), str(command.session_id)),
            )
            connection.commit()
            return SessionDeletionClaim(
                command.command_id, command.session_id, row["generation"], revision, owner_id, fencing_token
            )

    def advance_deletion(
        self, claim: SessionDeletionClaim, state: SessionDeletionState, *, detail: str = ""
    ) -> SessionDeletionReceipt:
        allowed = {
            SessionDeletionState.CLAIMED: SessionDeletionState.REFERENCES_RELEASING,
            SessionDeletionState.REFERENCES_RELEASING: SessionDeletionState.METADATA_TOMBSTONED,
            SessionDeletionState.METADATA_TOMBSTONED: SessionDeletionState.BLOBS_RECLAIMING,
            SessionDeletionState.BLOBS_RECLAIMING: SessionDeletionState.DIRECTORY_RETIRING,
            SessionDeletionState.DIRECTORY_RETIRING: SessionDeletionState.SETTLED,
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM deletions WHERE command_id = ?", (claim.command_id,)).fetchone()
            if (
                row is None
                or row["revision"] != claim.revision
                or row["owner_id"] != claim.owner_id
                or row["fencing_token"] != claim.fencing_token
            ):
                raise SessionLifecycleConflictError("Session deletion claim is stale")
            current = SessionDeletionState(row["state"])
            if state is not SessionDeletionState.IN_DOUBT and allowed.get(current) is not state:
                raise SessionLifecycleConflictError("Session deletion transition is invalid")
            revision = row["revision"] + 1
            now = self._now()
            connection.execute(
                "UPDATE deletions SET revision = ?, state = ?, updated_at = ?, detail = ? WHERE command_id = ?",
                (revision, state.value, now, detail, claim.command_id),
            )
            if state is SessionDeletionState.SETTLED:
                connection.execute(
                    "UPDATE sessions SET revision = revision + 1, state = ?, updated_at = ? WHERE session_id = ?",
                    (SessionLifecycleState.TOMBSTONED.value, now, str(claim.session_id)),
                )
            connection.commit()
            return SessionDeletionReceipt(
                claim.command_id, claim.session_id, state, revision, datetime.fromisoformat(now), detail
            )

    def _expected(
        self, connection: sqlite3.Connection, session_id: SessionId, generation: int, revision: int
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)).fetchone()
        if row is None or row["generation"] != generation or row["revision"] != revision:
            raise SessionLifecycleConflictError("Session lifecycle expectation is stale")
        return row

    @staticmethod
    def _decode_blockers(payload: str) -> tuple[SessionBlockerKind, ...]:
        raw = json.loads(payload)
        if type(raw) is not list or any(type(item) is not str for item in raw):
            raise RuntimeError("Session blocker record is corrupt")
        blockers = tuple(SessionBlockerKind(item) for item in raw)
        if tuple(sorted(set(blockers), key=lambda item: item.value)) != blockers:
            raise RuntimeError("Session blocker record is not canonical")
        return blockers

    def _snapshot(self, row: sqlite3.Row) -> SessionEligibilitySnapshot:
        return SessionEligibilitySnapshot(
            SessionId(row["session_id"]),
            row["generation"],
            row["revision"],
            SessionLifecycleState(row["state"]),
            self._decode_blockers(row["blockers"]),
            None if row["terminal_at"] is None else datetime.fromisoformat(row["terminal_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS lifecycle_schema (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), schema TEXT NOT NULL
            );
            INSERT OR IGNORE INTO lifecycle_schema(singleton, schema) VALUES (1, 'mote.session-lifecycle/v2');
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation > 0),
                revision INTEGER NOT NULL CHECK(revision > 0), state TEXT NOT NULL,
                blockers TEXT NOT NULL, terminal_at TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deletions (
                command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, generation INTEGER NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0), state TEXT NOT NULL, owner_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK(fencing_token > 0), requested_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_retention_scan ON sessions(state, terminal_at, session_id);
            CREATE INDEX IF NOT EXISTS session_deletions_scan ON deletions(state, updated_at, command_id);
            """)
        schema = connection.execute("SELECT schema FROM lifecycle_schema WHERE singleton = 1").fetchone()
        if schema is None or schema["schema"] != SESSION_LIFECYCLE_SCHEMA:
            connection.close()
            raise RuntimeError("Session lifecycle schema is unsupported")
        connection.commit()
        if os.name == "posix":
            os.chmod(self._path, 0o600)
        return connection

    def _now(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Session lifecycle clock must return an aware instant")
        return now.astimezone(timezone.utc).isoformat()


__all__ = ["SESSION_LIFECYCLE_SCHEMA", "SessionLifecycleConflictError", "SessionLifecycleStore"]
