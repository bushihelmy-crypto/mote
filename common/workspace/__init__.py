"""Workspace layout ownership + disk-layer TTL cleanup.

:class:`WorkspaceStore` is the single owner of the on-disk workspace layout (all
per-session artifacts co-located under one session directory); :mod:`cleanup`
sweeps that tree on a session-unit lifecycle model. See :mod:`.store` and
:mod:`.cleanup` for the full contracts.
"""

from mote.common.workspace.cleanup import CleanupStats, run_cleanup_if_due, sweep_workspace
from mote.common.workspace.store import ArtifactKind, WorkspaceStore

__all__ = [
    "ArtifactKind",
    "WorkspaceStore",
    "CleanupStats",
    "run_cleanup_if_due",
    "sweep_workspace",
]
