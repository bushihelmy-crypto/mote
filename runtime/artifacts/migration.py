"""Crash-safe migration into the workspace-wide Artifact repository."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.artifacts import ArtifactPublication, ArtifactRetention
from mote.runtime.fileops.artifact_budgets import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.fileops.artifact_repository import ArtifactRepository
from mote.runtime.session.migrations.legacy import decode_session_meta_record

from .layout import ArtifactRepositoryLayout, project_artifact_identity
from .ownership import ArtifactOwnership
from .repository_blobs import ArtifactRepositoryBlobStore
from .store import ARTIFACT_INDEX_FILENAME, DurableArtifactStore
from .transfer import ArtifactRevisionTransfer

_MAXIMUM_META_LINE_BYTES = 1_024 * 1_024
_LEGACY_SCOPES_DIRNAME = ".artifact_scopes"


class ArtifactMigrationSourceError(RuntimeError):
    """Legacy durable data cannot be moved without guessing its ownership."""


@dataclass(frozen=True, slots=True)
class ArtifactMigrationReport:
    scanned_sources: int = 0
    skipped_sources: int = 0
    migrated_revisions: int = 0
    migrated_publications: int = 0
    failures: tuple[str, ...] = ()
    blocked_session_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _MigrationSource:
    name: str
    index_path: Path
    blobs_path: Path
    source_ownership: ArtifactOwnership
    destination_ownership: ArtifactOwnership
    retentions: tuple[ArtifactRetention, ...]
    session_id: str = ""


class _MigrationCatalog:
    """Skip unchanged source indexes while detecting old-writer changes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def is_current(self, source: Path) -> bool:
        fingerprint = self._fingerprint(source)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT size, mtime_ns FROM migrated_sources WHERE source_path = ?",
                (str(source),),
            ).fetchone()
        return row == fingerprint

    def record_current(self, source: Path) -> None:
        size, mtime_ns = self._fingerprint(source)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO migrated_sources (source_path, size, mtime_ns) "
                "VALUES (?, ?, ?) ON CONFLICT(source_path) DO UPDATE SET "
                "size = excluded.size, mtime_ns = excluded.mtime_ns",
                (str(source), size, mtime_ns),
            )

    @staticmethod
    def _fingerprint(source: Path) -> tuple[int, int]:
        stat = source.stat()
        return stat.st_size, stat.st_mtime_ns

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS migrated_sources ("
            "source_path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
            "mtime_ns INTEGER NOT NULL)"
        )
        return connection


class LegacyArtifactMigrator:
    """Move every legacy Artifact owner into one catalog and one CAS.

    A move always imports and verifies the destination before releasing the
    exact source fact. Both halves are idempotent. A crash at any boundary
    therefore leaves a readable source, a readable destination, or both, and a
    later run deterministically completes the same move.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._layout = ArtifactRepositoryLayout(Path(workspace_root))
        self._sessions_root = self._layout.workspace_root / ".agent_sessions"
        self._legacy_scopes_root = self._layout.workspace_root / _LEGACY_SCOPES_DIRNAME
        self._catalog = _MigrationCatalog(self._layout.migration_index_path)

    def migrate(self) -> ArtifactMigrationReport:
        self._layout.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        scanned = 0
        skipped = 0
        migrated_revisions = 0
        migrated_publications = 0
        failures: list[str] = []
        blocked_session_ids: set[str] = set()
        sources, discovery_failures, discovery_blocked = self._discover_sources()
        failures.extend(discovery_failures)
        blocked_session_ids.update(discovery_blocked)
        for source in sources:
            scanned += 1
            try:
                if self._catalog.is_current(source.index_path):
                    skipped += 1
                    continue
                revisions, publications = self._migrate_source(source)
                self._catalog.record_current(source.index_path)
                migrated_revisions += revisions
                migrated_publications += publications
            except Exception as exc:
                failures.append(f"{source.name}: {type(exc).__name__}: {exc}")
                if source.session_id:
                    blocked_session_ids.add(source.session_id)
        if sources:
            collector_ownership = sources[0].destination_ownership
        else:
            collector_ownership = ArtifactOwnership.standalone()
        self._layout.open(collector_ownership).collector.collect()
        return ArtifactMigrationReport(
            scanned_sources=scanned,
            skipped_sources=skipped,
            migrated_revisions=migrated_revisions,
            migrated_publications=migrated_publications,
            failures=tuple(failures),
            blocked_session_ids=tuple(sorted(blocked_session_ids)),
        )

    def _discover_sources(
        self,
    ) -> tuple[tuple[_MigrationSource, ...], tuple[str, ...], tuple[str, ...],]:
        sources: list[_MigrationSource] = []
        failures: list[str] = []
        blocked_session_ids: list[str] = []
        if self._sessions_root.is_dir():
            for session_dir in sorted(path for path in self._sessions_root.iterdir() if path.is_dir()):
                index_path = session_dir / ARTIFACT_INDEX_FILENAME
                if not index_path.is_file():
                    continue
                try:
                    project_id = self._session_project_id(session_dir, index_path)
                except Exception as exc:
                    failures.append(f"session:{session_dir.name}: {type(exc).__name__}: {exc}")
                    blocked_session_ids.append(session_dir.name)
                    continue
                destination_ownership = ArtifactOwnership(
                    session_id=session_dir.name,
                    project_id=project_id,
                )
                sources.append(
                    _MigrationSource(
                        name=f"session:{session_dir.name}",
                        index_path=index_path,
                        blobs_path=session_dir / "blobs",
                        source_ownership=self._source_ownership(
                            index_path,
                            destination_ownership,
                        ),
                        destination_ownership=destination_ownership,
                        retentions=tuple(ArtifactRetention),
                        session_id=session_dir.name,
                    )
                )

        projects_root = self._legacy_scopes_root / "projects"
        if projects_root.is_dir():
            for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
                index_path = project_dir / ARTIFACT_INDEX_FILENAME
                if index_path.is_file():
                    destination_ownership = ArtifactOwnership(
                        session_id=f"migration-project-{project_dir.name}",
                        project_id=project_dir.name,
                    )
                    sources.append(
                        _MigrationSource(
                            name=f"project:{project_dir.name}",
                            index_path=index_path,
                            blobs_path=project_dir / "blobs",
                            source_ownership=self._source_ownership(
                                index_path,
                                destination_ownership,
                            ),
                            destination_ownership=destination_ownership,
                            retentions=(ArtifactRetention.PROJECT,),
                        )
                    )

        pinned_dir = self._legacy_scopes_root / "pinned"
        pinned_index = pinned_dir / ARTIFACT_INDEX_FILENAME
        if pinned_index.is_file():
            destination_ownership = ArtifactOwnership(
                session_id="migration-pinned",
                project_id="migration-pinned",
            )
            sources.append(
                _MigrationSource(
                    name="pinned:global",
                    index_path=pinned_index,
                    blobs_path=pinned_dir / "blobs",
                    source_ownership=self._source_ownership(
                        pinned_index,
                        destination_ownership,
                    ),
                    destination_ownership=destination_ownership,
                    retentions=(ArtifactRetention.PINNED,),
                )
            )
        return tuple(sources), tuple(failures), tuple(blocked_session_ids)

    def _migrate_source(self, source_spec: _MigrationSource) -> tuple[int, int]:
        source_repository = ArtifactRepository(
            source_spec.blobs_path,
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        )
        source = DurableArtifactStore(
            source_spec.index_path,
            ArtifactRepositoryBlobStore(source_repository),
            ownership=source_spec.source_ownership,
        )
        destination = self._layout.open(source_spec.destination_ownership).store
        transfers = source.export_revisions(source_spec.retentions)
        publications = source.export_pending_publications(source_spec.retentions)
        return self._move(source, destination, transfers, publications)

    @staticmethod
    def _move(
        source: DurableArtifactStore,
        destination: DurableArtifactStore,
        transfers: tuple[ArtifactRevisionTransfer, ...],
        publications: tuple[ArtifactPublication, ...],
    ) -> tuple[int, int]:
        moved_revisions = 0
        moved_publications = 0
        for transfer in transfers:
            retention = transfer.revision.representations[0].retention
            destination.import_transfer(transfer, retention)
            if source.release_transfer(transfer):
                moved_revisions += 1
        for publication in publications:
            destination.import_publication(publication)
            if source.discard_publication(publication):
                moved_publications += 1
        return moved_revisions, moved_publications

    def _session_project_id(self, session_dir: Path, index_path: Path) -> str:
        if not self._has_project_retention(index_path):
            return f"unassigned-session-{session_dir.name}"
        project_root = self._read_project_root(session_dir / "rollout.jsonl")
        return project_artifact_identity(project_root)

    @staticmethod
    def _source_ownership(
        index_path: Path,
        destination_ownership: ArtifactOwnership,
    ) -> ArtifactOwnership:
        try:
            connection = sqlite3.connect(
                f"file:{index_path}?mode=ro",
                uri=True,
                timeout=5,
            )
            try:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master " "WHERE type = 'table' AND name = 'artifact_owners'"
                ).fetchone()
                if table is None:
                    return ArtifactOwnership.standalone()
                standalone = connection.execute(
                    "SELECT 1 FROM artifact_owners " "WHERE owner_id = 'standalone' LIMIT 1"
                ).fetchone()
                if standalone is not None:
                    return ArtifactOwnership.standalone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ArtifactMigrationSourceError("legacy Artifact ownership is unreadable") from exc
        return destination_ownership

    @staticmethod
    def _has_project_retention(index_path: Path) -> bool:
        try:
            connection = sqlite3.connect(
                f"file:{index_path}?mode=ro",
                uri=True,
                timeout=5,
            )
            try:
                for table in (
                    "artifact_representations",
                    "artifact_publication_outbox",
                ):
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master " "WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    if exists is None:
                        continue
                    row = connection.execute(
                        f"SELECT 1 FROM {table} WHERE retention = ? LIMIT 1",
                        (ArtifactRetention.PROJECT.value,),
                    ).fetchone()
                    if row is not None:
                        return True
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ArtifactMigrationSourceError("legacy Artifact index is unreadable") from exc
        return False

    @staticmethod
    def _read_project_root(rollout: Path) -> str:
        try:
            with rollout.open("rb") as stream:
                raw = stream.readline(_MAXIMUM_META_LINE_BYTES + 1)
        except OSError as exc:
            raise ArtifactMigrationSourceError("legacy PROJECT Artifact has no readable session metadata") from exc
        if len(raw) > _MAXIMUM_META_LINE_BYTES or not raw.endswith(b"\n"):
            raise ArtifactMigrationSourceError("legacy session metadata line is missing or oversized")
        event = decode_session_meta_record(raw)
        if event is None or not event.project_root:
            raise ArtifactMigrationSourceError("legacy PROJECT Artifact has no authoritative project root")
        return event.project_root


__all__ = [
    "ArtifactMigrationReport",
    "ArtifactMigrationSourceError",
    "LegacyArtifactMigrator",
]
