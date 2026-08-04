"""Offline, one-way Event subscription state v1 to v2 migration."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mote.runtime.events.backends.subscription_state import SQLiteSubscriptionStateStore
from mote.runtime.persistence import disk_io


@dataclass(frozen=True, slots=True)
class EventSubscriptionMigrationReceipt:
    source_digest: str
    checkpoint_count: int
    dead_letter_count: int


def migrate_event_subscription_v1(path: Path) -> EventSubscriptionMigrationReceipt:
    source = path.read_bytes()
    source_digest = hashlib.sha256(source).hexdigest()
    legacy = sqlite3.connect(path)
    legacy.row_factory = sqlite3.Row
    try:
        if legacy.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Event subscription migration requires strict v1 input")
        expected = {
            "subscription_checkpoints": ("subscription", "stream_id", "sequence", "updated_at"),
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
        }
        for table, columns in expected.items():
            if tuple(row[1] for row in legacy.execute(f"PRAGMA table_info({table})")) != columns:
                raise ValueError(f"Event subscription v1 table {table} is not canonical")
        checkpoints = tuple(legacy.execute("SELECT * FROM subscription_checkpoints ORDER BY subscription, stream_id"))
        dead_letters = tuple(
            legacy.execute("SELECT * FROM subscription_dead_letters ORDER BY subscription, stream_id, sequence")
        )
    finally:
        legacy.close()
    candidate = path.with_name(f".{path.name}.v2-candidate")
    evidence = path.with_name(f"{path.name}.v1-evidence-{source_digest}.sqlite3")
    if evidence.exists() and evidence.read_bytes() != source:
        raise ValueError("Event subscription migration evidence conflicts with source digest")
    if not evidence.exists():
        disk_io.atomic_write(evidence, source, fsync=True)
    candidate.unlink(missing_ok=True)
    store = SQLiteSubscriptionStateStore(candidate)
    store._open_sync()
    store._close_sync()
    target = sqlite3.connect(candidate)
    try:
        target.execute("BEGIN IMMEDIATE")
        subscriptions = sorted(
            {row["subscription"] for row in checkpoints} | {row["subscription"] for row in dead_letters}
        )
        target.executemany(
            "INSERT INTO subscription_owners(subscription, owner_id, generation, fencing_token) VALUES (?, ?, 1, 1)",
            tuple((subscription, "migration-v1") for subscription in subscriptions),
        )
        target.executemany(
            "INSERT INTO subscription_checkpoints(subscription, stream_id, sequence, owner_id, generation, fencing_token, updated_at) "
            "VALUES (?, ?, ?, 'migration-v1', 1, 1, ?)",
            tuple((row["subscription"], row["stream_id"], row["sequence"], row["updated_at"]) for row in checkpoints),
        )
        target.executemany(
            "INSERT INTO subscription_dead_letters(subscription, stream_id, sequence, event_id, envelope_record, attempts, "
            "error, first_failed_at, last_failed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(tuple(row) for row in dead_letters),
        )
        target.commit()
    finally:
        target.close()
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(candidate, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return EventSubscriptionMigrationReceipt(source_digest, len(checkpoints), len(dead_letters))


__all__ = ["EventSubscriptionMigrationReceipt", "migrate_event_subscription_v1"]
