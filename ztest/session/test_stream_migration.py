from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.events.envelope import StreamId
from mote.product.migrations.session_stream import (
    SessionMigrationConflict,
    SessionMigrationSourceKind,
    activate_session_v2_candidate,
    build_session_v2_candidate,
    inventory_session_v1,
    retire_session_migration_evidence,
)
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.session.codec import encode_session_event, session_stream_id
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog


def test_v1_session_stream_migrates_checksum_chain_and_activates_v2(tmp_path) -> None:
    session_id = "session-1"
    directory = tmp_path / session_id
    path = directory / "rollout.jsonl"
    journal = LocalEventJournal(path, StreamId(session_stream_id(session_id)))
    fact = encode_session_event(
        SessionMetaEvent(session_id=session_id, role_class="test.Role", toolset_manifest=()),
        session_id=session_id,
    )
    journal.append_committed(
        StreamId(session_stream_id(session_id)),
        (replace(fact, schema_version=1),),
        expected_version=0,
    )
    journal.writer.flush_inline()
    (directory / "runtime.lease").write_text("lease", encoding="utf-8")
    (directory / "checkpoint.json").write_text("checkpoint", encoding="utf-8")
    inventory = inventory_session_v1(directory)
    candidate = build_session_v2_candidate(directory, inventory)
    assert not (directory / "stream-manifest.json").exists()
    receipt = activate_session_v2_candidate(
        directory,
        candidate,
        activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert receipt.record_count == 1
    restored = SessionLog(session_id, base_dir=str(tmp_path))
    assert tuple(restored.iter_events())[0].session_id == session_id
    assert tuple(tmp_path.joinpath(session_id, ".migration-evidence").glob("v1-*.jsonl"))
    assert {source.kind for source in inventory.sources} >= {
        SessionMigrationSourceKind.ROLLOUT,
        SessionMigrationSourceKind.LEASE,
        SessionMigrationSourceKind.CHECKPOINT,
    }
    assert receipt.projection_digest == inventory.projection_digest


def test_existing_v1_stream_without_manifest_fails_closed(tmp_path) -> None:
    session_id = "session-1"
    directory = tmp_path / session_id
    directory.mkdir()
    (directory / "rollout.jsonl").write_bytes(b"legacy\n")
    with pytest.raises(RuntimeError, match="explicit v1 to v2 migration"):
        SessionLog(session_id, base_dir=str(tmp_path)).committed_version


def test_session_inventory_conflict_blocks_candidate_without_mutating_source(
    tmp_path,
) -> None:
    directory = tmp_path / "session-1"
    directory.mkdir()
    source = directory / "rollout.jsonl"
    source.write_bytes(b"corrupt\n")
    before = source.read_bytes()

    inventory = inventory_session_v1(directory)

    assert SessionMigrationConflict.CORRUPT_STREAM in inventory.conflicts
    with pytest.raises(RuntimeError, match="conflicts forbid"):
        build_session_v2_candidate(directory, inventory)
    assert source.read_bytes() == before


def test_session_activation_tamper_and_unactivated_candidate_fail_closed(
    tmp_path,
) -> None:
    session_id = "session-1"
    directory = tmp_path / session_id
    path = directory / "rollout.jsonl"
    journal = LocalEventJournal(path, StreamId(session_stream_id(session_id)))
    fact = encode_session_event(
        SessionMetaEvent(session_id=session_id, role_class="test.Role", toolset_manifest=()),
        session_id=session_id,
    )
    journal.append_committed(
        StreamId(session_stream_id(session_id)),
        (replace(fact, schema_version=1),),
        expected_version=0,
    )
    journal.writer.flush_inline()
    candidate = build_session_v2_candidate(directory, inventory_session_v1(directory))
    with pytest.raises(RuntimeError, match="explicit v1 to v2 migration"):
        SessionLog(session_id, base_dir=str(tmp_path)).committed_version
    activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    activate_session_v2_candidate(directory, candidate, activated_at=activated_at)
    manifest_path = directory / "stream-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["projection_digest"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="projection digest mismatch"):
        SessionLog(session_id, base_dir=str(tmp_path)).committed_version


def test_session_migration_evidence_retires_only_after_180_days(tmp_path) -> None:
    session_id = "session-1"
    directory = tmp_path / session_id
    path = directory / "rollout.jsonl"
    journal = LocalEventJournal(path, StreamId(session_stream_id(session_id)))
    fact = encode_session_event(
        SessionMetaEvent(session_id=session_id, role_class="test.Role", toolset_manifest=()),
        session_id=session_id,
    )
    journal.append_committed(
        StreamId(session_stream_id(session_id)),
        (replace(fact, schema_version=1),),
        expected_version=0,
    )
    journal.writer.flush_inline()
    activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = build_session_v2_candidate(directory, inventory_session_v1(directory))
    activate_session_v2_candidate(directory, candidate, activated_at=activated_at)
    with pytest.raises(RuntimeError, match="retention has not elapsed"):
        retire_session_migration_evidence(
            directory,
            now=activated_at + timedelta(days=179),
            authority_id="maintenance-1",
        )
    receipt = retire_session_migration_evidence(
        directory, now=activated_at + timedelta(days=180), authority_id="maintenance-1"
    )
    assert receipt.session_id == session_id
    assert receipt.authority_id == "maintenance-1"
    assert not (directory / ".migration-evidence").exists()
    assert SessionLog(session_id, base_dir=str(tmp_path)).committed_version == 1
