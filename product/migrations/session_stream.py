"""Offline inventory, inactive candidate, and atomic Session-v2 activation."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from mote.contracts.events.envelope import JsonValue, thaw_json
from mote.runtime.events.journal import decode_event_record, encode_event_record
from mote.runtime.persistence import disk_io
from mote.runtime.session.log import SESSION_STREAM_MANIFEST_SCHEMA

SESSION_MIGRATION_INVENTORY_SCHEMA = "mote.session-migration-inventory/v1"
SESSION_ARTIFACT_EDGE_SCHEMA = "mote.session-artifact-edges/v2"
MAX_MIGRATION_FACTS = 10_000
MAX_MIGRATION_SECONDS = 5.0


class SessionMigrationSourceKind(StrEnum):
    ROLLOUT = "rollout"
    DIRECTORY = "directory"
    LEASE = "lease"
    CHECKPOINT = "checkpoint"
    ARTIFACT_ROOT = "artifact_root"


class SessionMigrationConflict(StrEnum):
    CORRUPT_STREAM = "corrupt_stream"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSAFE_PATH = "unsafe_path"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True, slots=True)
class SessionMigrationSource:
    relative_path: str
    kind: SessionMigrationSourceKind
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class SessionMigrationInventory:
    session_id: str
    source_digest: str
    record_count: int
    sources: tuple[SessionMigrationSource, ...]
    artifact_digests: tuple[str, ...]
    projection_digest: str
    conflicts: tuple[SessionMigrationConflict, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionMigrationCandidate:
    inventory: SessionMigrationInventory
    stream_path: Path
    stream_digest: str
    artifact_edges_path: Path
    artifact_edges_digest: str


@dataclass(frozen=True, slots=True)
class SessionMigrationReceipt:
    session_id: str
    record_count: int
    source_digest: str
    projection_digest: str


@dataclass(frozen=True, slots=True)
class SessionMigrationEvidenceRetirementReceipt:
    session_id: str
    authority_id: str
    evidence_digest: str
    retired_at: datetime


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _artifact_digests(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        digest = value.get("digest")
        content_ref = value.get("content_ref")
        if type(digest) is str and type(content_ref) is dict and digest:
            found.add(digest)
        for item in value.values():
            found.update(_artifact_digests(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.update(_artifact_digests(item))
    return found


def inventory_session_v1(session_dir: Path) -> SessionMigrationInventory:
    session_dir = session_dir.resolve()
    source = session_dir / "rollout.jsonl"
    data = source.read_bytes()
    started = time.monotonic()
    conflicts: list[SessionMigrationConflict] = []
    sources: list[SessionMigrationSource] = []
    artifact_digests: set[str] = set()
    projection: list[tuple[int, str, str]] = []
    session_id: str | None = None
    record_count = 0
    for number, line in enumerate(data.splitlines(keepends=True), start=1):
        if number > MAX_MIGRATION_FACTS or time.monotonic() - started > MAX_MIGRATION_SECONDS:
            conflicts.append(SessionMigrationConflict.LIMIT_EXCEEDED)
            break
        if not line.endswith(b"\n"):
            conflicts.append(SessionMigrationConflict.CORRUPT_STREAM)
            break
        try:
            envelope = decode_event_record(line)
        except Exception:
            conflicts.append(SessionMigrationConflict.CORRUPT_STREAM)
            break
        if envelope.schema_version != 1:
            conflicts.append(SessionMigrationConflict.UNSUPPORTED_VERSION)
        if envelope.session_id is None or not str(envelope.stream_id).endswith(envelope.session_id):
            conflicts.append(SessionMigrationConflict.IDENTITY_MISMATCH)
        elif session_id is None:
            session_id = envelope.session_id
        elif session_id != envelope.session_id:
            conflicts.append(SessionMigrationConflict.IDENTITY_MISMATCH)
        artifact_digests.update(_artifact_digests(envelope.payload))
        projection.append(
            (
                envelope.sequence,
                str(envelope.event_type),
                _digest(
                    json.dumps(
                        thaw_json(cast(JsonValue, envelope.payload)),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ),
            )
        )
        record_count += 1
    if not data or session_id is None:
        conflicts.append(SessionMigrationConflict.CORRUPT_STREAM)
    for path in sorted(item for item in session_dir.rglob("*") if item != source):
        if path.is_symlink():
            conflicts.append(SessionMigrationConflict.UNSAFE_PATH)
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(session_dir).as_posix()
        raw = path.read_bytes()
        lowered = relative.lower()
        kind = (
            SessionMigrationSourceKind.LEASE
            if "lease" in lowered
            else (
                SessionMigrationSourceKind.CHECKPOINT
                if "checkpoint" in lowered
                else (
                    SessionMigrationSourceKind.ARTIFACT_ROOT
                    if "artifact" in lowered
                    else SessionMigrationSourceKind.DIRECTORY
                )
            )
        )
        sources.append(SessionMigrationSource(relative, kind, _digest(raw), len(raw)))
    sources.append(
        SessionMigrationSource(
            "rollout.jsonl",
            SessionMigrationSourceKind.ROLLOUT,
            _digest(data),
            len(data),
        )
    )
    projection_digest = _digest(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode())
    return SessionMigrationInventory(
        session_id or "unknown",
        _digest(data),
        record_count,
        tuple(sorted(sources, key=lambda item: item.relative_path)),
        tuple(sorted(artifact_digests)),
        projection_digest,
        tuple(dict.fromkeys(conflicts)),
    )


def build_session_v2_candidate(
    session_dir: Path,
    inventory: SessionMigrationInventory,
) -> SessionMigrationCandidate:
    if inventory.conflicts:
        raise RuntimeError("Session migration conflicts forbid candidate construction")
    source = session_dir / "rollout.jsonl"
    data = source.read_bytes()
    if _digest(data) != inventory.source_digest:
        raise ValueError("Session migration source changed after inventory")
    previous: str | None = None
    encoded: list[bytes] = []
    for line in data.splitlines(keepends=True):
        envelope = decode_event_record(line)
        record, previous = encode_event_record(replace(envelope, schema_version=2), previous)
        decoded = decode_event_record(record)
        if decoded.session_id != inventory.session_id or decoded.schema_version != 2:
            raise RuntimeError("Session v2 candidate read-back identity failed")
        encoded.append(record)
    stream_path = session_dir / ".migration-candidate" / "rollout.v2.jsonl"
    stream_data = b"".join(encoded)
    disk_io.atomic_write(stream_path, stream_data, fsync=True)
    edges = {
        "schema": SESSION_ARTIFACT_EDGE_SCHEMA,
        "session_id": inventory.session_id,
        "artifact_digests": list(inventory.artifact_digests),
        "projection_digest": inventory.projection_digest,
    }
    edges_path = stream_path.parent / "artifact-edges.v2.json"
    edges_data = json.dumps(edges, sort_keys=True, separators=(",", ":")).encode()
    disk_io.atomic_write(edges_path, edges_data, fsync=True)
    if stream_path.read_bytes() != stream_data or json.loads(edges_path.read_bytes()) != edges:
        raise RuntimeError("Session migration candidate read-back failed")
    return SessionMigrationCandidate(
        inventory,
        stream_path,
        _digest(stream_data),
        edges_path,
        _digest(edges_data),
    )


def activate_session_v2_candidate(
    session_dir: Path,
    candidate: SessionMigrationCandidate,
    *,
    activated_at: datetime,
) -> SessionMigrationReceipt:
    if activated_at.tzinfo is None or activated_at.utcoffset() is None:
        raise ValueError("Session activation instant must be timezone-aware")
    source = session_dir / "rollout.jsonl"
    if _digest(source.read_bytes()) != candidate.inventory.source_digest:
        raise ValueError("Session migration source changed before activation")
    stream = candidate.stream_path.read_bytes()
    edges = candidate.artifact_edges_path.read_bytes()
    if _digest(stream) != candidate.stream_digest or _digest(edges) != candidate.artifact_edges_digest:
        raise ValueError("Session migration candidate changed after read-back")
    evidence = (
        session_dir / ".migration-evidence" / f"v1-{candidate.inventory.source_digest.removeprefix('sha256:')}.jsonl"
    )
    disk_io.atomic_write(evidence, source.read_bytes(), fsync=True)
    disk_io.atomic_write(session_dir / "artifact-edges.v2.json", edges, fsync=True)
    disk_io.atomic_write(source, stream, fsync=True)
    manifest = {
        "schema": SESSION_STREAM_MANIFEST_SCHEMA,
        "session_id": candidate.inventory.session_id,
        "source_digest": candidate.inventory.source_digest,
        "candidate_digest": candidate.stream_digest,
        "candidate_size": len(stream),
        "artifact_edges_digest": candidate.artifact_edges_digest,
        "projection_digest": candidate.inventory.projection_digest,
        "record_count": candidate.inventory.record_count,
        "evidence_retention_days": 180,
        "legacy_production_reader": "retired",
        "activation_kind": "migrated",
        "activated_at": activated_at.isoformat(),
        "retire_after": (activated_at + timedelta(days=180)).isoformat(),
    }
    disk_io.atomic_write(
        session_dir / "stream-manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        fsync=True,
    )
    candidate.stream_path.unlink()
    candidate.artifact_edges_path.unlink()
    descriptor = os.open(session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return SessionMigrationReceipt(
        candidate.inventory.session_id,
        candidate.inventory.record_count,
        candidate.inventory.source_digest,
        candidate.inventory.projection_digest,
    )


def retire_session_migration_evidence(
    session_dir: Path,
    *,
    now: datetime,
    authority_id: str,
) -> SessionMigrationEvidenceRetirementReceipt:
    """Retire only raw v1 evidence after the activated retention boundary."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Session evidence retirement instant must be timezone-aware")
    if not authority_id:
        raise ValueError("Session evidence retirement requires an authority identity")
    manifest_path = session_dir / "stream-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        retire_after = datetime.fromisoformat(manifest["retire_after"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Session activation evidence is unavailable") from exc
    if (
        type(manifest) is not dict
        or manifest.get("schema") != SESSION_STREAM_MANIFEST_SCHEMA
        or manifest.get("activation_kind") != "migrated"
        or type(manifest.get("session_id")) is not str
        or retire_after.tzinfo is None
    ):
        raise RuntimeError("Session activation evidence is not an activated migration")
    if now < retire_after:
        raise RuntimeError("Session migration evidence retention has not elapsed")
    evidence_dir = session_dir / ".migration-evidence"
    try:
        entries = tuple(sorted(evidence_dir.iterdir()))
    except OSError as exc:
        raise RuntimeError("Session migration evidence is unavailable") from exc
    if not entries or any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise RuntimeError("Session migration evidence layout is invalid")
    evidence_digest = _digest(b"".join(entry.read_bytes() for entry in entries))
    for entry in entries:
        entry.unlink()
    evidence_dir.rmdir()
    descriptor = os.open(session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return SessionMigrationEvidenceRetirementReceipt(
        session_id=manifest["session_id"],
        authority_id=authority_id,
        evidence_digest=evidence_digest,
        retired_at=now,
    )


__all__ = [
    "MAX_MIGRATION_FACTS",
    "MAX_MIGRATION_SECONDS",
    "SESSION_ARTIFACT_EDGE_SCHEMA",
    "SESSION_MIGRATION_INVENTORY_SCHEMA",
    "SessionMigrationCandidate",
    "SessionMigrationConflict",
    "SessionMigrationInventory",
    "SessionMigrationReceipt",
    "SessionMigrationEvidenceRetirementReceipt",
    "SessionMigrationSource",
    "SessionMigrationSourceKind",
    "activate_session_v2_candidate",
    "build_session_v2_candidate",
    "inventory_session_v1",
    "retire_session_migration_evidence",
]
