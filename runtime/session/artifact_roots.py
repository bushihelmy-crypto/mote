"""Workspace-wide FileOps reachability for the shared Artifact CAS."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mote.contracts.artifact import ArtifactContentRef
from mote.runtime.artifacts.repository import ArtifactRepository
from mote.runtime.fileops import FileOperations
from mote.runtime.session.codec import iter_file_operations_events
from mote.runtime.session.log import SessionLog


class SessionFileOpsArtifactRoots:
    """Project every session journal and cursor registry into CAS roots."""

    def __init__(
        self,
        sessions_root: Path,
        repository: ArtifactRepository,
        *,
        excluded_session_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._sessions_root = Path(sessions_root)
        self._repository = repository
        self._excluded_session_ids = excluded_session_ids

    def artifact_roots(self) -> tuple[ArtifactContentRef, ...]:
        roots: dict[str, ArtifactContentRef] = {}
        for operations in self._operations():
            for ref in operations.artifact_roots():
                roots[ref.digest] = ref
        return tuple(roots[digest] for digest in sorted(roots))

    def prune_artifact_metadata(self, reachable: Sequence[ArtifactContentRef]) -> None:
        for operations in self._operations():
            operations.prune_artifact_metadata(reachable)

    def _operations(self) -> tuple[FileOperations, ...]:
        operations = []
        if not self._sessions_root.is_dir():
            return ()
        for session_dir in self._sessions_root.iterdir():
            rollout = session_dir / "rollout.jsonl"
            if session_dir.name in self._excluded_session_ids or not session_dir.is_dir() or not rollout.is_file():
                continue
            log = SessionLog(session_dir.name, base_dir=str(self._sessions_root))
            operations.append(
                FileOperations(
                    session_id=session_dir.name,
                    journal_path=rollout,
                    get_project_root=str,
                    artifact_repository=self._repository,
                    artifact_lifecycle_root=session_dir / "artifact-lifecycle",
                    lock_root=log.runtime_root / "file-locks",
                    event_source=lambda log=log: iter_file_operations_events(log.iter_events()),
                )
            )
        return tuple(operations)


__all__ = ["SessionFileOpsArtifactRoots"]
