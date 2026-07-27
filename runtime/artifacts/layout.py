"""Workspace-wide Artifact catalog and CAS layout."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mote.runtime.fileops.artifact_budgets import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.fileops.artifact_repository import ArtifactRepository

from .ownership import ArtifactOwnership
from .repository_blobs import ArtifactRepositoryBlobStore
from .store import ARTIFACT_INDEX_FILENAME, DurableArtifactStore
from .store_gc import ArtifactStoreGarbageCollector

ARTIFACT_REPOSITORY_DIRNAME = ".artifacts"


def project_artifact_identity(project_root: str | Path) -> str:
    canonical = str(Path(project_root).expanduser().resolve(strict=False))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactRepositoryBundle:
    store: DurableArtifactStore
    repository: ArtifactRepository
    collector: ArtifactStoreGarbageCollector


class ArtifactRepositoryLayout:
    """Resolve the one logical catalog and one physical CAS in a workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.root = self.workspace_root / ARTIFACT_REPOSITORY_DIRNAME

    @property
    def index_path(self) -> Path:
        return self.root / ARTIFACT_INDEX_FILENAME

    @property
    def blobs_path(self) -> Path:
        return self.root / "blobs"

    @property
    def migration_index_path(self) -> Path:
        return self.root / "migrations.sqlite3"

    def ownership(
        self,
        *,
        session_id: str,
        project_root: str | Path,
    ) -> ArtifactOwnership:
        return ArtifactOwnership(
            session_id=session_id,
            project_id=project_artifact_identity(project_root),
        )

    def open(self, ownership: ArtifactOwnership) -> ArtifactRepositoryBundle:
        repository = ArtifactRepository(
            self.blobs_path,
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        )
        store = DurableArtifactStore(
            self.index_path,
            ArtifactRepositoryBlobStore(repository),
            ownership=ownership,
        )
        return ArtifactRepositoryBundle(
            store=store,
            repository=repository,
            collector=ArtifactStoreGarbageCollector(store, repository),
        )


__all__ = [
    "ARTIFACT_REPOSITORY_DIRNAME",
    "ArtifactRepositoryBundle",
    "ArtifactRepositoryLayout",
    "project_artifact_identity",
]
