"""Runtime SessionWorkspace — owner of the on-disk workspace layout.

Every artifact a session produces lives UNDER one session directory::

    {root}/.agent_sessions/{session_id}/
        rollout.jsonl     # the append-only truth source (liveness signal)
        artifact-lifecycle/ # FileOps mutation metadata (payloads live in shared CAS)
        tool_results/     # large tool-result overflow
        task_outputs/     # background-task stdout logs
        ledger/           # tool-effect idempotency ledger (crash-replay guard)

Co-locating artifacts under the session directory makes cleanup orphan-proof by
construction: removing a session directory removes its entire footprint
atomically. This class is the *only* place that turns the layout constants in
the injected root into concrete paths — every writer resolves its
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

from mote.contracts.task.models import SessionId
from mote.runtime.session.layout import SessionLayout

PathLike = Union[str, Path]


class SessionSpace(str, Enum):
    """The per-session artifact directories the store hands out.

    A ``str`` enum so the value doubles as the on-disk subdirectory name and can
    be compared/serialized directly. Adding a new artifact kind is a one-line
    change here — the single extension point for the layout.
    """

    TOOL_RESULTS = "tool_results"
    TASK_OUTPUTS = "task_outputs"
    LEDGER = "ledger"


class SessionWorkspace:
    """Resolves every workspace path from one root, per the centralized layout."""

    def __init__(
        self,
        root: PathLike | None = None,
        layout: SessionLayout = SessionLayout(),
    ) -> None:
        if root is None:
            raise ValueError("SessionWorkspace requires an explicit root")
        self._root = Path(root)
        self._layout = layout

    @property
    def root(self) -> Path:
        """The workspace root under which everything lives."""
        return self._root

    @property
    def sessions_root(self) -> Path:
        """Directory holding every session directory (``root/.agent_sessions``)."""
        return self._root / self._layout.sessions_dir

    def session_dir(self, session_id: str) -> Path:
        """The one directory that owns *session_id*'s entire footprint.

        An empty id falls back to the shared ``default`` bucket so unattributed
        artifacts still have a stable, sweepable home.
        """
        return self.sessions_root / (session_id or self._layout.default_session)

    def rollout_path(self, session_id: str) -> Path:
        """The session's rollout log — its truth source and liveness signal."""
        return self.session_dir(session_id) / self._layout.rollout_file

    def output_directory(self, session_id: SessionId) -> Path:
        return self.space(session_id, SessionSpace.TASK_OUTPUTS)

    def space(self, session_id: str, kind: str | SessionSpace) -> Path:
        """The directory for one *kind* of artifact under *session_id*.

        The single seam every artifact writer uses to find where to write, so
        the layout stays defined in exactly one place.
        """
        name = kind.value if isinstance(kind, SessionSpace) else kind
        return self.session_dir(session_id) / name

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


__all__ = ["SessionLayout", "SessionSpace", "SessionWorkspace"]
