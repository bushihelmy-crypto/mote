from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import mote.runtime.session.migrations.v1 as migration_v1
from mote.contracts.fileops.events import FileHistoryImportedEvent
from mote.runtime.fileops.artifact_lifecycle import ArtifactReservationState
from mote.runtime.fileops.artifact_repository import ArtifactRepository
from mote.runtime.session.migrations.v1 import (
    V1MigrationError,
    V1SnapshotBackend,
    V1SnapshotOperation,
    import_v1_file_snapshot,
    parse_v1_file_snapshot,
)


def _payload(**overrides):
    content = b"before image\n"
    payload = {
        "path": "/project/file.txt",
        "operation": "update",
        "pre_hash": hashlib.sha256(content).hexdigest(),
        "pre_size": len(content),
        "display_path": "file.txt",
        "tool": "Edit",
        "backend": "blob",
    }
    payload.update(overrides)
    return payload


def _write_blob(session_dir: Path, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    path = session_dir / "blobs" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return digest


def _repository(artifact_root: Path) -> ArtifactRepository:
    return ArtifactRepository(artifact_root, hard_limit_bytes=1024 * 1024)


def _import(snapshot, session_dir: Path, repository: ArtifactRepository):
    reservation = repository.reserve(
        snapshot.pre_size,
        "session-migration-v1:test",
        60,
    )
    fact = import_v1_file_snapshot(
        snapshot,
        session_id="session-v1",
        source_ordinal=7,
        recorded_at="2026-01-02T03:04:05",
        session_dir=session_dir,
        repository=repository,
        reservation=reservation,
    )
    return fact, reservation


def test_parse_v1_file_snapshot_is_exact() -> None:
    parsed = parse_v1_file_snapshot(_payload())

    assert parsed.operation == V1SnapshotOperation.UPDATE
    assert parsed.backend == V1SnapshotBackend.BLOB
    assert parsed.path == "/project/file.txt"
    assert parsed.tool == "Edit"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {key: value for key, value in _payload().items() if key != "tool"},
        {**_payload(), "extra": True},
        _payload(path=""),
        _payload(display_path=""),
        _payload(tool=None),
        _payload(pre_size=True),
        _payload(pre_size=-1),
        _payload(operation="replace"),
        _payload(backend="filesystem"),
        _payload(pre_hash="A" * 64),
        _payload(pre_hash=None),
        _payload(operation="create"),
        _payload(operation="create", pre_hash=None, pre_size=1),
    ],
)
def test_parse_v1_file_snapshot_rejects_noncanonical_payload(payload) -> None:
    with pytest.raises(V1MigrationError):
        parse_v1_file_snapshot(payload)


def test_parse_v1_create_requires_formal_absence() -> None:
    parsed = parse_v1_file_snapshot(_payload(operation="create", pre_hash=None, pre_size=0))

    assert parsed.operation == V1SnapshotOperation.CREATE
    assert parsed.pre_hash is None


def test_blob_before_image_is_verified_and_imported(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    content = b"\x00legacy bytes\xff\n"
    digest = _write_blob(session_dir, content)
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    artifact_root = tmp_path / "canonical"
    repository = _repository(artifact_root)

    fact, reservation = _import(snapshot, session_dir, repository)

    assert isinstance(fact, FileHistoryImportedEvent)
    assert fact.source_schema_version == 1
    assert fact.before is not None
    assert fact.before.digest == hashlib.sha256(content).hexdigest()
    assert fact.before.size == len(content)
    assert repository.read_bytes(fact.before) == content
    assert fact.operation == "update"
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


def test_import_id_is_deterministic_and_source_order_sensitive(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    content = b"same"
    digest = _write_blob(session_dir, content)
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    repository = _repository(tmp_path / "canonical")

    def import_at(source_ordinal):
        reservation = repository.reserve(
            snapshot.pre_size,
            f"session-migration-v1:{source_ordinal}",
            60,
        )
        return import_v1_file_snapshot(
            snapshot,
            session_id="s",
            source_ordinal=source_ordinal,
            recorded_at="2026-01-01T00:00:00",
            session_dir=session_dir,
            repository=repository,
            reservation=reservation,
        )

    first = import_at(1)
    repeated = import_at(1)
    second = import_at(2)

    assert first.import_id == repeated.import_id
    assert first.import_id != second.import_id


def test_create_import_has_no_before_image_and_reads_no_legacy_store(tmp_path: Path) -> None:
    snapshot = parse_v1_file_snapshot(_payload(operation="create", pre_hash=None, pre_size=0))

    repository = _repository(tmp_path / "canonical")
    fact, reservation = _import(snapshot, tmp_path / "missing-session", repository)

    assert fact.before is None
    assert fact.operation == "create"
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


def test_import_requires_an_exact_fresh_reservation(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    content = b"budget"
    digest = _write_blob(session_dir, content)
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    repository = _repository(tmp_path / "canonical")
    oversized = repository.reserve(
        len(content) + 1,
        "session-migration-v1:oversized",
        60,
    )

    with pytest.raises(ValueError, match="exact v1 before-image budget"):
        import_v1_file_snapshot(
            snapshot,
            session_id="s",
            source_ordinal=1,
            recorded_at="2026-01-01T00:00:00",
            session_dir=session_dir,
            repository=repository,
            reservation=oversized,
        )

    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(oversized.reservation_id).state == ArtifactReservationState.ACTIVE


def test_reservation_is_released_only_after_fact_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_dir = tmp_path / "session"
    content = b"ordered"
    digest = _write_blob(session_dir, content)
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    repository = _repository(tmp_path / "canonical")
    reservation = repository.reserve(
        snapshot.pre_size,
        "session-migration-v1:ordering",
        60,
    )
    original = migration_v1.FileHistoryImportedEvent

    def construct_fact(**kwargs):
        assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.ACTIVE
        return original(**kwargs)

    monkeypatch.setattr(migration_v1, "FileHistoryImportedEvent", construct_fact)

    fact = import_v1_file_snapshot(
        snapshot,
        session_id="s",
        source_ordinal=1,
        recorded_at="2026-01-01T00:00:00",
        session_dir=session_dir,
        repository=repository,
        reservation=reservation,
    )

    assert isinstance(fact, original)
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


def test_fact_construction_failure_releases_live_stage_reservation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_dir = tmp_path / "session"
    content = b"sealed-before-fact"
    digest = _write_blob(session_dir, content)
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    repository = _repository(tmp_path / "canonical")
    reservation = repository.reserve(
        snapshot.pre_size,
        "session-migration-v1:fact-failure",
        60,
    )

    def reject_fact(**kwargs):
        raise RuntimeError("injected fact construction failure")

    monkeypatch.setattr(migration_v1, "FileHistoryImportedEvent", reject_fact)

    with pytest.raises(RuntimeError, match="fact construction failure"):
        import_v1_file_snapshot(
            snapshot,
            session_id="s",
            source_ordinal=1,
            recorded_at="2026-01-01T00:00:00",
            session_dir=session_dir,
            repository=repository,
            reservation=reservation,
        )

    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.health().active_reservations == 0
    assert tuple(repository.incoming_root.iterdir()) == ()
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


@pytest.mark.parametrize("failure", ["missing", "size", "digest"])
def test_blob_import_fails_closed_without_sealed_artifact(
    tmp_path: Path,
    failure: str,
) -> None:
    session_dir = tmp_path / "session"
    content = b"expected"
    digest = hashlib.sha256(content).hexdigest()
    if failure != "missing":
        stored = content + (b"!" if failure == "size" else b"")
        path = session_dir / "blobs" / digest[:2] / digest
        path.parent.mkdir(parents=True)
        path.write_bytes(stored if failure == "size" else b"x" + content[1:])
    snapshot = parse_v1_file_snapshot(_payload(pre_hash=digest, pre_size=len(content)))
    artifact_root = tmp_path / "canonical"
    repository = _repository(artifact_root)
    reservation = repository.reserve(
        snapshot.pre_size,
        "session-migration-v1:failure",
        60,
    )

    with pytest.raises(V1MigrationError):
        import_v1_file_snapshot(
            snapshot,
            session_id="s",
            source_ordinal=1,
            recorded_at="2026-01-01T00:00:00",
            session_dir=session_dir,
            repository=repository,
            reservation=reservation,
        )

    assert list(artifact_root.glob("[0-9a-f][0-9a-f]/*")) == []
    assert list((artifact_root / ".incoming").iterdir()) == []
    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_git_before_image_is_verified_and_converted_to_sha256(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    git_dir = session_dir / "git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(git_dir)],
        check=True,
        capture_output=True,
    )
    content = b"legacy git object\x00\xff"
    written = subprocess.run(
        ["git", "--git-dir", str(git_dir), "hash-object", "-w", "--stdin"],
        input=content,
        check=True,
        capture_output=True,
    )
    object_id = written.stdout.decode("ascii").strip()
    snapshot = parse_v1_file_snapshot(
        _payload(
            backend="git",
            pre_hash=object_id,
            pre_size=len(content),
        )
    )
    artifact_root = tmp_path / "canonical"
    repository = _repository(artifact_root)

    fact, reservation = _import(snapshot, session_dir, repository)

    assert fact.before is not None
    assert fact.before.digest == hashlib.sha256(content).hexdigest()
    assert repository.read_bytes(fact.before) == content
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_git_before_image_size_mismatch_leaves_no_artifact(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    git_dir = session_dir / "git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(git_dir)],
        check=True,
        capture_output=True,
    )
    content = b"git bytes"
    written = subprocess.run(
        ["git", "--git-dir", str(git_dir), "hash-object", "-w", "--stdin"],
        input=content,
        check=True,
        capture_output=True,
    )
    snapshot = parse_v1_file_snapshot(
        _payload(
            backend="git",
            pre_hash=written.stdout.decode("ascii").strip(),
            pre_size=len(content) - 1,
        )
    )
    artifact_root = tmp_path / "canonical"
    repository = _repository(artifact_root)
    reservation = repository.reserve(
        snapshot.pre_size,
        "session-migration-v1:git-failure",
        60,
    )

    with pytest.raises(V1MigrationError):
        import_v1_file_snapshot(
            snapshot,
            session_id="s",
            source_ordinal=1,
            recorded_at="2026-01-01T00:00:00",
            session_dir=session_dir,
            repository=repository,
            reservation=reservation,
        )

    assert list(artifact_root.glob("[0-9a-f][0-9a-f]/*")) == []
    assert list((artifact_root / ".incoming").iterdir()) == []
    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(reservation.reservation_id).state == ArtifactReservationState.RELEASED
