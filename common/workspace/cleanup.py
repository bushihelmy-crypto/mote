"""Time-based cleanup of the workspace disk layer.

The workspace grows unbounded otherwise: every session leaves a rollout log,
file-snapshot blobs, and (for large results / background tasks) overflow
artifacts on disk, and nothing ever reclaimed them. This module sweeps that
tree on a session-unit lifecycle model.

**The session is the lifecycle unit.** The session's ``rollout.jsonl`` mtime is
the single liveness signal, read against two thresholds:

* **session_ttl_days** — a session untouched for longer than this is *dead*; its
  whole directory (rollout + blobs + every artifact) is removed atomically.
* **artifact_ttl_days** — a still-alive-but-stale session keeps its rollout and
  blobs (the record) but sheds its large overflow artifacts (``tool_results/`` +
  ``task_outputs/``), which are the bulky, least-essential part.

A threshold of ``<= 0`` means *never expire on that tier* (the "don't clean"
config form). The current (or a just-resumed) session is excluded so a live run
is never swept out from under itself. Leftover pre-co-location top-level trees
are mtime-pruned one last time (the migration sweep).

The sweep is best-effort and side-effect-local: a per-session failure is logged
and skipped, never raised, so one unremovable entry can't abort the whole run.
:func:`run_cleanup_if_due` throttles the sweep to at most once per 24h via a
stamp file so session start stays cheap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mote.common.const import WORKSPACE_CLEANUP_STAMP
from mote.common.disk import disk_io
from mote.common.logs import logger
from mote.common.workspace.store import ArtifactKind, WorkspaceStore

_DAY_SECONDS = 86_400.0
#: Run the sweep at most once per this interval (throttled by the stamp file).
_THROTTLE_SECONDS = _DAY_SECONDS

#: The overflow artifact kinds shed on the artifact tier (rollout + blobs stay).
_ARTIFACT_TIER_KINDS = (ArtifactKind.TOOL_RESULTS, ArtifactKind.TASK_OUTPUTS)


@dataclass
class CleanupStats:
    """What one sweep touched (returned for logging / tests)."""

    scanned: int = 0
    sessions_removed: int = 0
    artifact_dirs_removed: int = 0
    legacy_dirs_removed: int = 0


def _age_days(path: Path, now: float) -> Optional[float]:
    """Age of *path* in days from its mtime, or ``None`` when it has no mtime."""
    mtime = disk_io.mtime_seconds(path)
    if mtime is None:
        return None
    return (now - mtime) / _DAY_SECONDS


def _sweep_session(
    store: WorkspaceStore,
    session_id: str,
    *,
    session_ttl_days: int,
    artifact_ttl_days: int,
    now: float,
    stats: CleanupStats,
) -> None:
    """Apply the two-tier TTL to one session (liveness = rollout mtime)."""
    session_dir = store.session_dir(session_id)
    # Liveness = rollout mtime; fall back to the directory's own mtime for a
    # session dir with no (yet) rollout, and skip if neither is stat-able.
    age = _age_days(store.rollout_path(session_id), now)
    if age is None:
        age = _age_days(session_dir, now)
    if age is None:
        return

    if session_ttl_days > 0 and age > session_ttl_days:
        if disk_io.remove_tree(session_dir):
            stats.sessions_removed += 1
        return

    if artifact_ttl_days > 0 and age > artifact_ttl_days:
        removed_any = False
        for kind in _ARTIFACT_TIER_KINDS:
            if disk_io.remove_tree(store.space(session_id, kind)):
                removed_any = True
        if removed_any:
            stats.artifact_dirs_removed += 1


def _sweep_legacy(
    store: WorkspaceStore,
    *,
    artifact_ttl_days: int,
    now: float,
    stats: CleanupStats,
) -> None:
    """Mtime-prune leftover pre-co-location top-level artifact trees.

    Legacy trees carry no rollout, so they *are* pure artifacts and are gated on
    the artifact tier. Each per-session bucket is pruned by its own mtime; an
    emptied legacy root is then removed.
    """
    if artifact_ttl_days <= 0:
        return
    for legacy_root in store.legacy_dirs():
        if not legacy_root.is_dir():
            continue
        for child in legacy_root.iterdir():
            age = _age_days(child, now)
            if age is not None and age > artifact_ttl_days:
                if child.is_dir():
                    removed = disk_io.remove_tree(child)
                else:
                    disk_io.remove_file(child)
                    removed = True
                if removed:
                    stats.legacy_dirs_removed += 1
        # Drop the legacy root once it has been fully drained.
        try:
            if not any(legacy_root.iterdir()):
                legacy_root.rmdir()
        except OSError:
            pass


def sweep_workspace(
    store: WorkspaceStore,
    *,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    now: Optional[float] = None,
) -> CleanupStats:
    """Sweep the whole workspace once and return what was reclaimed.

    Args:
        store: The workspace layout owner to sweep.
        session_ttl_days: Days of rollout inactivity before a session dir is
            removed whole; ``<= 0`` never expires a session.
        artifact_ttl_days: Days of rollout inactivity before a still-alive
            session sheds its overflow artifacts; ``<= 0`` never sheds them.
        exclude_session_id: A session to leave untouched (the live/resumed one).
        now: Wall-clock seconds override (for tests); defaults to ``time.time()``.
    """
    now = time.time() if now is None else now
    stats = CleanupStats()
    for session_id in store.iter_session_ids():
        if session_id == exclude_session_id:
            continue
        stats.scanned += 1
        try:
            _sweep_session(
                store,
                session_id,
                session_ttl_days=session_ttl_days,
                artifact_ttl_days=artifact_ttl_days,
                now=now,
                stats=stats,
            )
        except OSError as exc:
            logger.warning(f"workspace cleanup: failed to sweep session {session_id}: {exc}")
    try:
        _sweep_legacy(store, artifact_ttl_days=artifact_ttl_days, now=now, stats=stats)
    except OSError as exc:
        logger.warning(f"workspace cleanup: legacy sweep failed: {exc}")
    return stats


def _stamp_path(store: WorkspaceStore) -> Path:
    return store.root / WORKSPACE_CLEANUP_STAMP


def run_cleanup_if_due(
    store: WorkspaceStore,
    *,
    enabled: bool,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    now: Optional[float] = None,
) -> Optional[CleanupStats]:
    """Run :func:`sweep_workspace`, at most once per 24h, when enabled.

    Returns the :class:`CleanupStats` when a sweep ran, or ``None`` when cleanup
    is disabled or was skipped by the throttle. Touches a stamp file under the
    workspace root before sweeping so a crash mid-sweep still defers the next
    attempt (the sweep itself is idempotent, so a re-run is harmless anyway).
    """
    if not enabled:
        return None

    now = time.time() if now is None else now
    stamp = _stamp_path(store)
    last = disk_io.mtime_seconds(stamp)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        return None

    try:
        disk_io.atomic_write(stamp, b"", fsync=False)
    except OSError as exc:
        # Can't record the stamp — run anyway (worst case: it runs again sooner).
        logger.debug(f"workspace cleanup: could not write stamp {stamp}: {exc}")

    stats = sweep_workspace(
        store,
        session_ttl_days=session_ttl_days,
        artifact_ttl_days=artifact_ttl_days,
        exclude_session_id=exclude_session_id,
        now=now,
    )
    logger.debug(
        "workspace cleanup: scanned={} sessions_removed={} artifact_dirs_removed={} legacy_dirs_removed={}".format(
            stats.scanned, stats.sessions_removed, stats.artifact_dirs_removed, stats.legacy_dirs_removed
        )
    )
    return stats


__all__ = ["CleanupStats", "run_cleanup_if_due", "sweep_workspace"]
