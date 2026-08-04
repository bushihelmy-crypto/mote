from __future__ import annotations

import sqlite3

import pytest

from mote.contracts.events.envelope import StreamId
from mote.contracts.ports.events.subscription import SubscriptionIdentity
from mote.product.migrations.event_subscription import migrate_event_subscription_v1
from mote.runtime.events.backends.subscription_state import SQLiteSubscriptionStateStore


def _v1(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE subscription_checkpoints (
            subscription TEXT NOT NULL, stream_id TEXT NOT NULL, sequence INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(subscription, stream_id)
        ) WITHOUT ROWID;
        CREATE TABLE subscription_dead_letters (
            subscription TEXT NOT NULL, stream_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL,
            envelope_record BLOB NOT NULL, attempts INTEGER NOT NULL, error TEXT NOT NULL,
            first_failed_at TEXT NOT NULL, last_failed_at TEXT NOT NULL,
            PRIMARY KEY(subscription, stream_id, sequence)
        ) WITHOUT ROWID;
        PRAGMA user_version = 1;
        INSERT INTO subscription_checkpoints VALUES ('mote.test.subscription', 'session/test', 7, '2026-01-01T00:00:00+00:00');
        """)
    connection.commit()
    connection.close()


@pytest.mark.asyncio
async def test_v1_migration_preserves_checkpoint_and_activates_only_v2(tmp_path) -> None:
    path = tmp_path / "subscriptions.sqlite3"
    _v1(path)
    receipt = migrate_event_subscription_v1(path)
    assert receipt.checkpoint_count == 1
    assert tuple(tmp_path.glob("subscriptions.sqlite3.v1-evidence-*.sqlite3"))
    store = SQLiteSubscriptionStateStore(path)
    await store.aopen()
    assert await store.load(SubscriptionIdentity("mote.test.subscription"), StreamId("session/test")) == 7
    lease = await store.claim_owner(SubscriptionIdentity("mote.test.subscription"), "current-owner")
    assert lease.generation == 2 and lease.fencing_token == 2
    await store.aclose()


def test_migration_rejects_unknown_or_noncanonical_source(tmp_path) -> None:
    path = tmp_path / "subscriptions.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 9")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="strict v1"):
        migrate_event_subscription_v1(path)
    assert path.read_bytes() == before
