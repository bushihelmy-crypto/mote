"""Test composition helper for the workspace Artifact CAS."""

from pathlib import Path

from mote.runtime.artifacts.budgets import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore
from mote.runtime.fileops.facade import FileOperations as RuntimeFileOperations
from mote.runtime.fileops.mutation.artifacts import (
    FileMutationArtifactRepository as RuntimeFileMutationArtifactRepository,
)


def FileOperations(**kwargs):
    journal_path = Path(kwargs["journal_path"])
    workspace_root = journal_path.parent.parent
    return RuntimeFileOperations(
        **kwargs,
        artifact_repository=ContentAddressedArtifactStore(
            workspace_root / ".artifacts" / "blobs",
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        ),
        artifact_lifecycle_root=journal_path.parent / "artifact-lifecycle",
    )


def FileMutationArtifactRepository(root: Path, *, hard_limit_bytes: int):
    root = Path(root)
    return RuntimeFileMutationArtifactRepository(
        ContentAddressedArtifactStore(root, hard_limit_bytes=hard_limit_bytes),
        lifecycle_root=root.parent / f"{root.name}-lifecycle",
        hard_limit_bytes=hard_limit_bytes,
    )


__all__ = ["FileMutationArtifactRepository", "FileOperations"]
