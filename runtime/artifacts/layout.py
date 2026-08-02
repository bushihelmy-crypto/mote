"""Workspace-wide Artifact catalog and CAS layout."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mote.runtime.artifacts.budgets import ARTIFACT_HARD_LIMIT_BYTES, ARTIFACT_MINIMUM_GC_AGE_NS
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore

from .gc import ArtifactGarbageCollector
from .ownership import ArtifactOwnership
from .pins import ArtifactPinRegistry
from .ports import ArtifactMetadataSource, ArtifactPinSource, ArtifactRootSource
from .repository_blobs import ContentAddressedArtifactBlobStore
from .store import ARTIFACT_INDEX_FILENAME, DurableArtifactStore

ARTIFACT_REPOSITORY_DIRNAME = ".artifacts"


def project_artifact_identity(project_root: str | Path) -> str:
    canonical = str(Path(project_root).expanduser().resolve(strict=False))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactRepositoryBundle:
    store: DurableArtifactStore
    repository: ContentAddressedArtifactStore
    collector: ArtifactGarbageCollector
    pins: ArtifactPinRegistry


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

    def open(
        self,
        ownership: ArtifactOwnership,
        *,
        root_sources: tuple[ArtifactRootSource, ...] = (),
        pin_sources: tuple[ArtifactPinSource, ...] = (),
        metadata_sources: tuple[ArtifactMetadataSource, ...] = (),
        minimum_gc_age_ns: int = ARTIFACT_MINIMUM_GC_AGE_NS,
    ) -> ArtifactRepositoryBundle:
        pins = ArtifactPinRegistry()
        for index, source in enumerate(pin_sources):
            pins.register_source(f"layout-source:{index}", source)
        repository = ContentAddressedArtifactStore(
            self.blobs_path,
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        )
        store = DurableArtifactStore(
            self.index_path,
            ContentAddressedArtifactBlobStore(repository),
            ownership=ownership,
        )
        return ArtifactRepositoryBundle(
            store=store,
            repository=repository,
            collector=ArtifactGarbageCollector(
                store,
                repository,
                root_sources=root_sources,
                pin_sources=(pins,),
                metadata_sources=metadata_sources,
                minimum_age_ns=minimum_gc_age_ns,
            ),
            pins=pins,
        )


__all__ = [
    "ARTIFACT_REPOSITORY_DIRNAME",
    "ArtifactRepositoryBundle",
    "ArtifactRepositoryLayout",
    "project_artifact_identity",
]
