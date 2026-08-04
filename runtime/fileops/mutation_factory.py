"""Canonical construction of immutable file mutation sets."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.errors import StaleSnapshotError
from mote.contracts.file.identity import AbsentVersion, FileSnapshot
from mote.contracts.file.mutations import (
    CreateMutation,
    DeleteMutation,
    Mutation,
    MutationSet,
    RecoveryPolicy,
    ReplaceMutation,
)
from mote.runtime.fileops.identity import name_identity, path_token, project_identity
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, encode_metadata_manifest
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, FileMutationArtifactRepository


class MutationFactory:
    """Freeze B1 artifacts and filesystem scope without publishing them."""

    def __init__(
        self,
        *,
        session_id: str,
        artifacts: FileMutationArtifactRepository,
        get_project_root: Callable[[], str],
    ) -> None:
        self.session_id = session_id
        self.artifacts = artifacts
        self.get_project_root = get_project_root

    def replacement(
        self,
        snapshot: FileSnapshot,
        content: bytes,
        *,
        scope: ArtifactWriteScope,
    ) -> ReplaceMutation:
        return self.replacement_from_artifact(
            snapshot,
            scope.put_bytes(content),
        )

    def replacement_from_artifact(
        self,
        snapshot: FileSnapshot,
        after: ContentIdentity,
    ) -> ReplaceMutation:
        self.artifacts.verify(snapshot.artifact)
        self.artifacts.verify(snapshot.metadata)
        self.artifacts.verify(after)
        return ReplaceMutation(before=snapshot, after=after)

    def creation(
        self,
        path: str | bytes,
        content: bytes,
        *,
        scope: ArtifactWriteScope,
    ) -> CreateMutation:
        return self.creation_from_artifact(
            path,
            scope.put_bytes(content),
            scope=scope,
        )

    def creation_from_artifact(
        self,
        path: str | bytes,
        after: ContentIdentity,
        *,
        scope: ArtifactWriteScope,
    ) -> CreateMutation:
        requested = path_token(path)
        parent = os.path.dirname(requested.native) or (b"." if isinstance(requested.native, bytes) else ".")
        if not os.path.isdir(parent):
            raise StaleSnapshotError(
                f"parent directory does not exist: {os.fsdecode(parent)}",
                path=os.fsdecode(parent),
            )
        if os.path.lexists(requested.native):
            raise StaleSnapshotError(
                f"{requested.display} already exists",
                path=requested.display,
            )
        root = path_token(self.get_project_root())
        resolved_root = os.path.realpath(os.fsdecode(root.native))
        resolved_parent = os.path.realpath(os.fsdecode(parent))
        try:
            common = os.path.commonpath((resolved_root, resolved_parent))
        except ValueError as exc:
            raise StaleSnapshotError(
                "create target is outside the project root",
                path=requested.display,
                cause=exc,
            ) from exc
        if common != resolved_root:
            raise StaleSnapshotError(
                "create target is outside the project root",
                path=requested.display,
            )
        self.artifacts.verify(after)
        metadata = scope.put_bytes(encode_metadata_manifest(PreservedMetadata.for_create()))
        return CreateMutation(
            requested_path=requested,
            target_path=requested,
            project_identity=project_identity(root),
            expected_version=AbsentVersion(name_identity(requested)),
            after=after,
            metadata=metadata,
        )

    def deletion(self, snapshot: FileSnapshot) -> DeleteMutation:
        self.artifacts.verify(snapshot.artifact)
        self.artifacts.verify(snapshot.metadata)
        return DeleteMutation(before=snapshot)

    def mutation_set(
        self,
        *,
        source: str,
        mutations: tuple[Mutation, ...],
        transaction_id: str | None = None,
    ) -> MutationSet:
        return MutationSet(
            transaction_id=transaction_id or uuid.uuid4().hex,
            session_id=self.session_id,
            source=source,
            mutations=mutations,
            recovery_policy=RecoveryPolicy.ROLLBACK_INCOMPLETE,
        )


__all__ = ["MutationFactory"]
