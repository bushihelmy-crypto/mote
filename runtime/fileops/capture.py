"""Single managed observation path for immutable file snapshots."""

from __future__ import annotations

from typing import Callable, Optional

from mote.contracts.file.identity import FileSnapshot, PathToken
from mote.runtime.fileops.control import ProjectOperationControl
from mote.runtime.fileops.identity import PathLike, project_identity
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope
from mote.runtime.fileops.snapshots import ObservedFileVersion, SealedSnapshotReader


class ManagedSnapshotCapture:
    """Acquires the project read barrier and seals exactly one file version."""

    def __init__(
        self,
        *,
        reader: SealedSnapshotReader,
        control: ProjectOperationControl,
        get_project_root: Callable[[], str],
    ) -> None:
        self.reader = reader
        self.control = control
        self._get_project_root = get_project_root

    def capture(
        self,
        path: PathLike,
        *,
        scope: ArtifactWriteScope,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
    ) -> FileSnapshot:
        project_root = self._get_project_root()
        project = project_identity(project_root)
        label = path.display if isinstance(path, PathToken) else str(path)
        with self.control.capture_lease(project=project, label=label):
            return self.reader.open_snapshot(
                path,
                scope=scope,
                project_root=project_root,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
            )

    def probe(self, path: PathLike) -> ObservedFileVersion:
        """Return one stable exact version without materializing an artifact."""
        project_root = self._get_project_root()
        project = project_identity(project_root)
        label = path.display if isinstance(path, PathToken) else str(path)
        with self.control.capture_lease(project=project, label=label):
            return self.reader.probe(path, project_root=project_root)


__all__ = ["ManagedSnapshotCapture"]
