"""Offline strict Artifact v1 inventory and single-generation v2 cutover."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mote.runtime.artifacts.store import ARTIFACT_ACTIVATION_MANIFEST_SCHEMA, ARTIFACT_INDEX_FILENAME
from mote.runtime.persistence import disk_io

_V1_TABLES = frozenset(
    {
        "artifact_representations",
        "artifact_publications",
        "artifact_lookup_keys",
        "artifact_owners",
        "artifact_publication_outbox",
        "artifact_publication_outbox_representations",
        "artifact_edges",
        "artifact_closure",
        "artifact_deletion_claims",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactMigrationInventory:
    source_digest: str
    artifact_count: int
    cas_count: int
    session_count: int
    orphan_digests: tuple[str, ...]
    missing_digests: tuple[str, ...]
    producer_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactMigrationReceipt:
    inventory: ArtifactMigrationInventory
    evidence_path: Path


@dataclass(frozen=True, slots=True)
class ArtifactLegacyRetirementReceipt:
    index_path: Path
    evidence_digest: str
    retired: bool


def inventory_artifact_v1(root: Path, *, sessions_root: Path) -> ArtifactMigrationInventory:
    root = Path(root)
    index = root / ARTIFACT_INDEX_FILENAME
    if not index.is_file():
        raise ValueError("Artifact migration index is absent")
    with sqlite3.connect(index) as connection:
        tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        if not tables or not tables.issubset(_V1_TABLES):
            raise ValueError("Artifact v1 inventory contains unknown tables")
        required = {"artifact_representations", "artifact_owners"}
        if not required.issubset(tables):
            raise ValueError("Artifact v1 inventory is incomplete")
        rows = connection.execute(
            "SELECT artifact_id, revision, digest, size FROM artifact_representations WHERE released = 0 "
            "ORDER BY artifact_id, revision, digest"
        ).fetchall()
        owner_rows = connection.execute(
            "SELECT DISTINCT owner_kind, owner_id FROM artifact_owners WHERE released = 0 "
            "ORDER BY owner_kind, owner_id"
        ).fetchall()
    referenced = {row[2]: row[3] for row in rows}
    cas: dict[str, int] = {}
    blobs = root / "blobs"
    if blobs.exists():
        for path in sorted(blobs.glob("[0-9a-f][0-9a-f]/[0-9a-f]*")):
            digest = path.name
            if len(digest) != 64 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Artifact CAS inventory contains corrupt content")
            cas[digest] = path.stat().st_size
    conflicts = tuple(sorted(digest for digest in referenced.keys() & cas.keys() if referenced[digest] != cas[digest]))
    if conflicts:
        raise ValueError("Artifact metadata and CAS sizes conflict")
    sessions = (
        tuple(sorted(path.name for path in Path(sessions_root).iterdir() if path.is_dir()))
        if Path(sessions_root).is_dir()
        else ()
    )
    producer_ids = tuple(sorted(f"{kind}:{owner_id}" for kind, owner_id in owner_rows))
    digest = hashlib.sha256()
    digest.update(index.read_bytes())
    for content_digest in sorted(cas):
        digest.update(content_digest.encode() + b"\0" + str(cas[content_digest]).encode() + b"\0")
    for session_id in sessions:
        digest.update(b"session\0" + session_id.encode() + b"\0")
    return ArtifactMigrationInventory(
        "sha256:" + digest.hexdigest(),
        len(rows),
        len(cas),
        len(sessions),
        tuple(sorted(cas.keys() - referenced.keys())),
        tuple(sorted(referenced.keys() - cas.keys())),
        producer_ids,
    )


def migrate_artifact_v1(root: Path, *, sessions_root: Path) -> ArtifactMigrationReceipt:
    root = Path(root)
    inventory = inventory_artifact_v1(root, sessions_root=sessions_root)
    if inventory.missing_digests:
        raise ValueError("Artifact migration is blocked by missing CAS content")
    candidate = root.with_name(f".{root.name}.v2-candidate")
    if candidate.exists():
        shutil.rmtree(candidate)

    def copy_candidate_file(source: str, target: str) -> str:
        if Path(source).name == ARTIFACT_INDEX_FILENAME:
            return shutil.copy2(source, target)
        os.link(source, target)
        return target

    shutil.copytree(root, candidate, copy_function=copy_candidate_file)
    index = candidate / ARTIFACT_INDEX_FILENAME
    with sqlite3.connect(index) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript("""
            DROP TABLE IF EXISTS artifact_edges;
            DROP TABLE IF EXISTS artifact_closure;
            DROP TABLE IF EXISTS artifact_deletion_claims;
            CREATE TABLE artifact_edges (
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, content_digest TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation > 0),
                PRIMARY KEY(owner_kind, owner_id, content_digest)
            );
            CREATE TABLE artifact_producers (
                producer_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation > 0),
                PRIMARY KEY(producer_id, generation)
            );
            CREATE TABLE artifact_closure_roots (
                producer_id TEXT NOT NULL, content_digest TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation > 0),
                PRIMARY KEY(producer_id, content_digest, generation)
            );
            CREATE TABLE artifact_closure (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL,
                producer_ids TEXT NOT NULL, committed_at TEXT NOT NULL
            );
            CREATE TABLE artifact_holds (
                hold_id TEXT PRIMARY KEY, kind TEXT NOT NULL, content_digest TEXT NOT NULL,
                owner_id TEXT NOT NULL, generation INTEGER NOT NULL, expires_at TEXT
            );
            CREATE TABLE artifact_edge_tombstones (
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, generation INTEGER NOT NULL,
                released_at TEXT NOT NULL, PRIMARY KEY(owner_kind, owner_id)
            );
            CREATE TABLE artifact_deletions (
                command_id TEXT PRIMARY KEY, content_digest TEXT NOT NULL, requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL, state TEXT NOT NULL, closure_generation INTEGER NOT NULL,
                revision INTEGER NOT NULL, owner_id TEXT NOT NULL, fencing_token INTEGER NOT NULL,
                updated_at TEXT NOT NULL, detail TEXT NOT NULL
            );
            """)
        owner_rows = connection.execute(
            "SELECT DISTINCT owner_kind, owner_id FROM artifact_owners WHERE released = 0 "
            "ORDER BY owner_kind, owner_id"
        ).fetchall()
        producer_roots: list[tuple[str, str, str]] = []
        for legacy_kind, owner_id in owner_rows:
            owner_kind = "session" if legacy_kind == "session" else "project"
            canonical_id = owner_id if legacy_kind != "global" else "global"
            producer_id = f"{legacy_kind}:{owner_id}"
            digests = connection.execute(
                "SELECT DISTINCT representations.digest FROM artifact_owners AS owners "
                "JOIN artifact_representations AS representations ON representations.artifact_id=owners.artifact_id "
                "AND representations.revision=owners.revision WHERE owners.owner_kind=? AND owners.owner_id=? "
                "AND owners.released=0 AND representations.released=0 ORDER BY representations.digest",
                (legacy_kind, owner_id),
            ).fetchall()
            connection.execute("INSERT INTO artifact_producers(producer_id,generation) VALUES (?,1)", (producer_id,))
            for (content_digest,) in digests:
                connection.execute(
                    "INSERT INTO artifact_edges(owner_kind,owner_id,content_digest,generation) VALUES (?,?,?,1)",
                    (owner_kind, canonical_id, content_digest),
                )
                producer_roots.append((producer_id, content_digest, owner_kind))
        producer_ids = tuple(sorted(f"{kind}:{owner_id}" for kind, owner_id in owner_rows))
        connection.execute(
            "INSERT INTO artifact_closure(singleton,generation,producer_ids,committed_at) VALUES (1,1,?,?)",
            (json.dumps(producer_ids), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    with sqlite3.connect(index) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Artifact v2 candidate read-back failed")
    disk_io.atomic_write(
        candidate / "activation-manifest.json",
        json.dumps(
            {
                "schema": ARTIFACT_ACTIVATION_MANIFEST_SCHEMA,
                "generation": 2,
                "source_digest": inventory.source_digest,
                "evidence_retention_days": 180,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        fsync=True,
    )
    if inventory_artifact_v1(root, sessions_root=sessions_root).source_digest != inventory.source_digest:
        raise ValueError("Artifact migration source changed after inventory")
    evidence = root.with_name(f"{root.name}.v1-evidence-{inventory.source_digest.removeprefix('sha256:')}")
    if evidence.exists():
        raise ValueError("Artifact migration evidence path already exists")
    os.replace(root, evidence)
    try:
        os.replace(candidate, root)
    except BaseException:
        os.replace(evidence, root)
        raise
    descriptor = os.open(root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ArtifactMigrationReceipt(inventory, evidence)


def retire_legacy_owner_table(
    index: Path, *, evidence_digest: str, evidence_path: Path
) -> ArtifactLegacyRetirementReceipt:
    """Physically retire ``artifact_owners`` after migration evidence is durable.

    This is a migration-only command: it refuses to run when evidence is
    missing, changed, or when any unreleased legacy fact remains.
    """
    evidence = Path(evidence_path)
    if not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_digest.removeprefix(
        "sha256:"
    ):
        raise ValueError("Artifact legacy retirement evidence is unavailable or changed")
    index = Path(index)
    with sqlite3.connect(index) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "artifact_owners" not in tables:
            return ArtifactLegacyRetirementReceipt(index, evidence_digest, True)
        if connection.execute("SELECT 1 FROM artifact_owners WHERE released=0 LIMIT 1").fetchone() is not None:
            raise ValueError("Artifact legacy owner facts are still active")
        connection.execute("DROP TABLE artifact_owners")
        connection.commit()
    return ArtifactLegacyRetirementReceipt(index, evidence_digest, True)


__all__ = [
    "ArtifactMigrationInventory",
    "ArtifactMigrationReceipt",
    "inventory_artifact_v1",
    "migrate_artifact_v1",
    "ArtifactLegacyRetirementReceipt",
    "retire_legacy_owner_table",
]
