"""Workspace layout ownership + disk-layer TTL cleanup.

:class:`SessionWorkspace` is the single owner of the on-disk workspace layout (all
per-session artifacts co-located under one session directory); :mod:`cleanup`
sweeps that tree on a session-unit lifecycle model. See :mod:`.store` and
:mod:`.cleanup` for the full contracts.
"""

from mote.runtime.session.layout import SessionLayout
from mote.runtime.session.workspace.cleanup import (
    CleanupStats,
    run_cleanup_if_due,
    run_cleanup_if_due_async,
    sweep_workspace,
    sweep_workspace_async,
)
from mote.runtime.session.workspace.cleanup_gate import WorkspaceCleanupGate
from mote.runtime.session.workspace.store import SessionSpace, SessionWorkspace

__all__ = [
    "SessionLayout",
    "SessionSpace",
    "SessionWorkspace",
    "WorkspaceCleanupGate",
    "CleanupStats",
    "run_cleanup_if_due",
    "run_cleanup_if_due_async",
    "sweep_workspace",
    "sweep_workspace_async",
]
