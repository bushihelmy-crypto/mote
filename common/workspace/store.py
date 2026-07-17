"""WorkspaceStore — the single owner of the on-disk workspace layout.

Every artifact a session produces lives UNDER one session directory::

    {root}/.agent_sessions/{session_id}/
        rollout.jsonl     # the append-only truth source (liveness signal)
        blobs/            # file snapshots
        tool_results/     # large tool-result overflow
        task_outputs/     # background-task stdout logs
        ledger/           # tool-effect idempotency ledger (crash-replay guard)

Co-locating artifacts under the session directory makes cleanup orphan-proof by
construction: removing a session directory removes its entire footprint
atomically. This class is the *only* place that turns the layout constants in
:mod:`mote.common.const.paths` into concrete paths — every writer resolves its
directory through :meth:`space` (or :meth:`rollout_path`), so the layout can
never drift across modules again.

The store holds only a root ``Path`` (no I/O in the constructor); it is cheap to
default-construct and safe to inject as one shared instance via the component
graph. A future pluggable backend (e.g. object storage) becomes a subclass here
rather than a change scattered across writers.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Iterator, Union

from mote.common.const import (
    DEFAULT_SESSION_BUCKET,
    DEFAULT_WORKSPACE_ROOT,
    LEGACY_TASK_OUTPUTS_SUBDIR,
    LEGACY_TOOL_RESULTS_SUBDIR,
    ROLLOUT_FILENAME,
    SESSIONS_SUBDIR,
)

PathLike = Union[str, Path]


class ArtifactKind(str, Enum):
    """The per-session artifact directories the store hands out.

    A ``str`` enum so the value doubles as the on-disk subdirectory name and can
    be compared/serialized directly. Adding a new artifact kind is a one-line
    change here — the single extension point for the layout.
    """

    TOOL_RESULTS = "tool_results"
    TASK_OUTPUTS = "task_outputs"
    BLOBS = "blobs"
    LEDGER = "ledger"


class WorkspaceStore:
    """Resolves every workspace path from one root, per the centralized layout."""

    def __init__(self, root: PathLike | None = None) -> None:
        self._root = Path(root) if root is not None else Path(DEFAULT_WORKSPACE_ROOT)

    @property
    def root(self) -> Path:
        """The workspace root under which everything lives."""
        return self._root

    @property
    def sessions_root(self) -> Path:
        """Directory holding every session directory (``root/.agent_sessions``)."""
        return self._root / SESSIONS_SUBDIR

    def session_dir(self, session_id: str) -> Path:
        """The one directory that owns *session_id*'s entire footprint.

        An empty id falls back to the shared ``default`` bucket so unattributed
        artifacts still have a stable, sweepable home.
        """
        return self.sessions_root / (session_id or DEFAULT_SESSION_BUCKET)

    def rollout_path(self, session_id: str) -> Path:
        """The session's rollout log — its truth source and liveness signal."""
        return self.session_dir(session_id) / ROLLOUT_FILENAME

    def space(self, session_id: str, kind: ArtifactKind) -> Path:
        """The directory for one *kind* of artifact under *session_id*.

        The single seam every artifact writer uses to find where to write, so
        the layout stays defined in exactly one place.
        """
        return self.session_dir(session_id) / kind.value

    def iter_session_ids(self) -> Iterator[str]:
        """Yield each existing session id (directory name under the sessions root).

        Side-effect-free: yields nothing when the sessions root does not exist.
        """
        root = self.sessions_root
        if not root.is_dir():
            return
        for child in root.iterdir():
            if child.is_dir():
                yield child.name

    def legacy_dirs(self) -> list[Path]:
        """Pre-co-location, top-level artifact trees (for the migration sweep).

        These are the old ``root/.tool_results`` / ``root/.task_outputs`` roots
        that predate co-location; the cleanup sweep mtime-prunes their leftover
        contents one last time. New writes never land here.
        """
        return [
            self._root / LEGACY_TOOL_RESULTS_SUBDIR,
            self._root / LEGACY_TASK_OUTPUTS_SUBDIR,
        ]


__all__ = ["ArtifactKind", "WorkspaceStore"]
