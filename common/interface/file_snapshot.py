"""FileSnapshotStore protocol — the before-image capture slice.

The narrow face the file-mutating tools (Write/Edit/NotebookEdit) use to record
a *before-image* of a file just before they overwrite it, without importing the
concrete ``session`` implementation.

Why a Protocol here (not in ``session``): the ``executor`` layer must
never import the ``roles`` layer (the strict downward-only layering rule). The
concrete ``FileSnapshotRecorder`` / blob store live in ``session`` and are
*injected* into the tools as a Role capability (``record_file_snapshot``); the
tools only depend on this structural face, so no upward import is introduced.

This mirrors :class:`metagpt.common.interface.SessionRecorder`: a leaf module
that only needs ``typing``, importable from anywhere without risking a cycle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FileSnapshotStore(Protocol):
    """Records a before-image of a file about to be mutated.

    Implemented by ``session.FileSnapshotRecorder`` (production) and any
    test double. Called by a file-mutating tool right before it writes, with the
    resolved absolute path. The store reads the current on-disk content (the
    "before" image), dedups it, and records a snapshot event. It owns its own
    enable/disable and persistence, and must be cheap and non-throwing from the
    tool's point of view.
    """

    def snapshot(self, full_path: str, *, tool: str = "") -> None:
        """Capture the current on-disk content of ``full_path`` as a before-image.

        Args:
            full_path: Resolved absolute path the tool is about to write.
            tool: Name of the tool performing the mutation (for the record).
        """
        ...
