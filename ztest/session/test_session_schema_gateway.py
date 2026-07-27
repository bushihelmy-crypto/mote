from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from pathlib import Path

import pytest

import mote.runtime.session.migrations.gateway as schema_gateway
from mote.contracts.fileops.events import FileHistoryImportedEvent
from mote.runtime.events.journal import decode_event_record
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import SCHEMA_VERSION, SessionMetaEvent
from mote.runtime.session.history import file_history, restore
from mote.runtime.session.log import SessionLog
from mote.runtime.session.migrations.gateway import SessionMigrationError


def _record(event_type: str, payload: dict, *, ts: str) -> str:
    return json.dumps(
        {"type": event_type, "ts": ts, "payload": payload},
        ensure_ascii=False,
    )


def _envelopes(log: SessionLog):
    return list(log.iter_events())


def _legacy_rollout(
    base: Path,
    *,
    session_id: str = "legacy",
    content: bytes = b"before\x00image\n",
) -> tuple[Path, Path]:
    session_dir = base / session_id
    session_dir.mkdir(parents=True)
    digest = hashlib.sha256(content).hexdigest()
    blob = session_dir / "blobs" / digest[:2] / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    target = base / "target.txt"
    target.write_bytes(b"after\n")
    lines = [
        _record(
            "session_meta",
            {
                "session_id": session_id,
                "schema_version": 1,
                "created_at": "2026-01-01T00:00:00",
                "working_dir": str(base),
            },
            ts="2026-01-01T00:00:00",
        ),
        _record(
            "future_event",
            {"kept": True},
            ts="2026-01-01T00:00:01",
        ),
        _record(
            "file_snapshot",
            {
                "path": str(target),
                "operation": "update",
                "pre_hash": digest,
                "pre_size": len(content),
                "display_path": "target.txt",
                "tool": "Edit",
                "backend": "blob",
            },
            ts="2026-01-01T00:00:02",
        ),
    ]
    rollout = session_dir / "rollout.jsonl"
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rollout, target


def test_session_log_atomically_migrates_v1_before_runtime_reads(tmp_path: Path) -> None:
    rollout, target = _legacy_rollout(tmp_path)
    log = SessionLog("legacy", base_dir=str(tmp_path))

    envelopes = _envelopes(log)
    meta = decode_session_event(envelopes[0])
    imported = decode_session_event(envelopes[-1])

    assert isinstance(meta, SessionMetaEvent)
    assert meta.schema_version == SCHEMA_VERSION
    assert [envelope.event_type for envelope in envelopes] == [
        "mote.session.session_meta",
        "mote.legacy.future_event",
        "mote.fileops.file_history_imported",
    ]
    assert envelopes[1].payload["kept"] is True
    assert envelopes[1].metadata["legacy_event_type"] == "future_event"
    assert isinstance(imported, FileHistoryImportedEvent)
    assert imported.before is not None
    assert imported.before.digest == hashlib.sha256(b"before\x00image\n").hexdigest()
    assert imported.source_ordinal == 3
    assert imported.recorded_at == "2026-01-01T00:00:02"
    assert not list(rollout.parent.glob(".rollout.jsonl.migrate-*.tmp"))
    backup = rollout.with_name("rollout.jsonl.schema-v1.backup")
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) & 0o222 == 0

    [entry] = file_history(log)[str(target)]
    assert entry.before == imported.before
    assert restore(log, str(target)) is True
    assert target.read_bytes() == b"before\x00image\n"


def test_failed_migration_leaves_original_rollout_untouched(tmp_path: Path) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    original = rollout.read_bytes()
    for payload in (rollout.parent / "blobs").glob("[0-9a-f][0-9a-f]/*"):
        payload.unlink()

    log = SessionLog("legacy", base_dir=str(tmp_path))
    with pytest.raises(SessionMigrationError):
        _envelopes(log)

    assert rollout.read_bytes() == original
    assert not list(rollout.parent.glob(".rollout.jsonl.migrate-*.tmp"))


def test_gateway_rejects_torn_rollout_without_replacement(tmp_path: Path) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    original = rollout.read_bytes().rstrip(b"\n")
    rollout.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="torn line"):
        _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    assert rollout.read_bytes() == original


def test_gateway_enforces_a_per_record_memory_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    monkeypatch.setattr(schema_gateway, "_MAX_RECORD_BYTES", 64)

    with pytest.raises(SessionMigrationError, match="migration bound"):
        _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    assert rollout.read_text(encoding="utf-8").splitlines()[0]


def test_replace_failure_keeps_verified_original_and_removes_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    original = rollout.read_bytes()

    def reject_replace(source, destination):
        raise OSError("injected replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(schema_gateway.os, "replace", reject_replace)
        with pytest.raises(SessionMigrationError, match="cannot migrate"):
            _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    assert rollout.read_bytes() == original
    assert not list(rollout.parent.glob(".rollout.jsonl.migrate-*.tmp"))
    backup = rollout.with_name("rollout.jsonl.schema-v1.backup")
    assert backup.read_bytes() == original

    envelopes = _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))
    assert isinstance(decode_session_event(envelopes[-1]), FileHistoryImportedEvent)


def test_existing_backup_must_match_source_byte_for_byte(tmp_path: Path) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    backup = rollout.with_name("rollout.jsonl.schema-v1.backup")
    backup.write_bytes(b"different")
    backup.chmod(stat.S_IRUSR)

    with pytest.raises(SessionMigrationError, match="backup differs"):
        _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))


def test_parent_fsync_failure_after_replace_is_idempotently_reentered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    original_fsync = schema_gateway.SessionSchemaGateway._fsync_directory
    calls = 0

    def fail_second_directory_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected parent fsync failure")
        original_fsync(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            schema_gateway.SessionSchemaGateway,
            "_fsync_directory",
            staticmethod(fail_second_directory_fsync),
        )
        with pytest.raises(SessionMigrationError, match="cannot migrate"):
            _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    first = decode_session_event(decode_event_record(rollout.read_text(encoding="utf-8").splitlines()[0]))
    assert isinstance(first, SessionMetaEvent)
    assert first.schema_version == SCHEMA_VERSION
    envelopes = _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))
    assert isinstance(decode_session_event(envelopes[-1]), FileHistoryImportedEvent)


def test_existing_backup_reestablishes_barriers_before_rollout_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rollout, _ = _legacy_rollout(tmp_path)
    original_fsync = schema_gateway.SessionSchemaGateway._fsync_directory
    replace_calls = 0

    def fail_backup_parent_fsync(path):
        raise OSError("injected backup parent fsync failure")

    real_replace = schema_gateway.os.replace

    def count_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(
            schema_gateway.SessionSchemaGateway,
            "_fsync_directory",
            staticmethod(fail_backup_parent_fsync),
        )
        patch.setattr(schema_gateway.os, "replace", count_replace)
        with pytest.raises(SessionMigrationError, match="cannot migrate"):
            _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    assert replace_calls == 0
    assert rollout.with_name("rollout.jsonl.schema-v1.backup").exists()

    fsync_calls = 0

    def count_directory_fsync(path):
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            schema_gateway.SessionSchemaGateway,
            "_fsync_directory",
            staticmethod(count_directory_fsync),
        )
        envelopes = _envelopes(SessionLog("legacy", base_dir=str(tmp_path)))

    assert fsync_calls == 2
    assert isinstance(decode_session_event(envelopes[-1]), FileHistoryImportedEvent)


def test_current_rollout_is_not_rewritten(tmp_path: Path) -> None:
    log = SessionLog("current", base_dir=str(tmp_path))

    asyncio.run(log.append(SessionMetaEvent(session_id="current")))
    original = log.path.read_bytes()

    [envelope] = _envelopes(log)
    event = decode_session_event(envelope)
    assert isinstance(event, SessionMetaEvent)
    assert event.schema_version == SCHEMA_VERSION
    assert log.path.read_bytes() == original
