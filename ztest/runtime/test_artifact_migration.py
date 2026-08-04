from __future__ import annotations

import hashlib
import sqlite3

from mote.contracts.artifact import ArtifactOwnerKind
from mote.product.migrations.artifact_store import inventory_artifact_v1, migrate_artifact_v1
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore
from mote.runtime.artifacts.repository_blobs import ContentAddressedArtifactBlobStore
from mote.runtime.artifacts.store import DurableArtifactStore


def _legacy_root(root):
    root.mkdir()
    content = b"legacy"
    digest = hashlib.sha256(content).hexdigest()
    blob = root / "blobs" / digest[:2] / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    with sqlite3.connect(root / "artifacts.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE artifact_representations (
                artifact_id TEXT, revision INTEGER, representation TEXT, kind TEXT, mime_type TEXT,
                content_ref TEXT, digest TEXT, size INTEGER, retention TEXT, sensitivity TEXT,
                suggested_name TEXT, released INTEGER DEFAULT 0
            );
            CREATE TABLE artifact_owners (
                artifact_id TEXT, revision INTEGER, owner_kind TEXT, owner_id TEXT,
                retention TEXT, released INTEGER DEFAULT 0
            );
            """)
        connection.execute(
            "INSERT INTO artifact_representations VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                "artifact",
                1,
                "original",
                "text",
                "text/plain",
                f"sha256:{digest}",
                digest,
                len(content),
                "session",
                "private",
                "",
            ),
        )
        connection.execute("INSERT INTO artifact_owners VALUES ('artifact',1,'session','session-a','session',0)")
        connection.commit()
    return digest


def test_artifact_root_v2_inventory_candidate_and_cutover(tmp_path):
    root = tmp_path / ".artifacts"
    sessions = tmp_path / ".agent_sessions"
    sessions.mkdir()
    (sessions / "session-a").mkdir()
    digest = _legacy_root(root)
    inventory = inventory_artifact_v1(root, sessions_root=sessions)
    assert inventory.orphan_digests == ()
    receipt = migrate_artifact_v1(root, sessions_root=sessions)
    assert receipt.evidence_path.is_dir()
    repository = ContentAddressedArtifactStore(root / "blobs", hard_limit_bytes=1024)
    store = DurableArtifactStore(root / "artifacts.sqlite3", ContentAddressedArtifactBlobStore(repository))
    edges = store.ownership_edges(owner_kind=ArtifactOwnerKind.SESSION, owner_id="session-a")
    assert tuple(edge.content_digest for edge in edges) == (digest,)


def test_artifact_inventory_reports_orphan_without_deleting_it(tmp_path):
    root = tmp_path / ".artifacts"
    sessions = tmp_path / ".agent_sessions"
    sessions.mkdir()
    _legacy_root(root)
    orphan = b"orphan"
    digest = hashlib.sha256(orphan).hexdigest()
    path = root / "blobs" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(orphan)
    inventory = inventory_artifact_v1(root, sessions_root=sessions)
    assert inventory.orphan_digests == (digest,)
    assert path.exists()
