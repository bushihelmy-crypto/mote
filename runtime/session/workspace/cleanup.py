"""Time-based cleanup of the workspace disk layer.

The workspace grows unbounded otherwise: every session leaves a rollout log,
shared-CAS references, and (for large results / background tasks) overflow
artifacts on disk, and nothing ever reclaimed them. This module sweeps that
tree on a session-unit lifecycle model.

The session's ``rollout.jsonl`` mtime is the liveness signal for session-owned
data, read against two thresholds:

* **session_ttl_days** — a session untouched for longer than this is *dead*.
  Its EPHEMERAL/SESSION ownership rows are released from the workspace Artifact
  catalog, unreachable Artifact bytes are collected, then its directory is
  removed. PROJECT/PINNED ownership is independent of the session directory.
* **artifact_ttl_days** — a still-alive-but-stale session keeps its rollout but
  sheds its large overflow artifacts (``tool_results/`` +
  ``task_outputs/``), which are the bulky, least-essential part.

A threshold of ``<= 0`` means *never expire on that tier* (the "don't clean"
config form). The current (or a just-resumed) session is also excluded so a
live run is never swept out from under itself.

The sweep is best-effort and side-effect-local: a per-session failure is logged
and skipped, never raised, so one unremovable entry can't abort the whole run.
:func:`run_cleanup_if_due` throttles the sweep to at most once per 24h via a
stamp file so session start stays cheap.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from mote.runtime.artifacts.layout import ArtifactRepositoryLayout
from mote.runtime.persistence import disk_io
from mote.runtime.session.artifact_roots import SessionFileOpsArtifactRoots
from mote.runtime.session.run_lease import RunLeaseStore
from mote.runtime.session.workspace.store import SessionSpace, SessionWorkspace
from mote.runtime.telemetry.logging import logger

_DAY_SECONDS = 86_400.0
#: Run the sweep at most once per this interval (throttled by the stamp file).
_THROTTLE_SECONDS = _DAY_SECONDS
_WORKSPACE_CLEANUP_STAMP = ".last_cleanup"

#: The overflow artifact kinds shed on the artifact tier (rollout stays).
_ARTIFACT_TIER_KINDS = (SessionSpace.TOOL_RESULTS, SessionSpace.TASK_OUTPUTS)


@dataclass
class CleanupStats:
    """What one sweep touched (returned for logging / tests)."""

    scanned: int = 0
    sessions_removed: int = 0
    artifact_dirs_removed: int = 0
    artifact_revisions_released: int = 0
    artifact_blobs_reclaimed: int = 0


def _prune_expired_session_artifacts(
    workspace_root: Path,
    session_dir: Path,
    session_id: str,
    mutation_guard: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Release one session owner, then collect the unified CAS closure."""
    layout = ArtifactRepositoryLayout(workspace_root)
    ownership = layout.ownership(
        session_id=session_id,
        project_root=session_dir,
    )
    repository = layout.open(ownership).repository
    fileops_artifacts = SessionFileOpsArtifactRoots(
        session_dir.parent,
        repository,
        excluded_session_ids=frozenset({session_id}),
    )
    bundle = layout.open(
        ownership,
        root_sources=(fileops_artifacts,),
        metadata_sources=(fileops_artifacts,),
        minimum_gc_age_ns=0,
    )
    if mutation_guard is not None:
        mutation_guard()
    released = bundle.store.release_session_scope()
    if mutation_guard is not None:
        mutation_guard()
    return released, bundle.collector.collect()


def _age_days(path: Path, now: float) -> Optional[float]:
    """Age of *path* in days from its mtime, or ``None`` when it has no mtime."""
    mtime = disk_io.mtime_seconds(path)
    if mtime is None:
        return None
    return (now - mtime) / _DAY_SECONDS


def _sweep_session(
    store: SessionWorkspace,
    session_id: str,
    *,
    session_ttl_days: int,
    artifact_ttl_days: int,
    now: float,
    stats: CleanupStats,
    mutation_guard: Callable[[], None] | None = None,
) -> None:
    """Apply the two-tier TTL to one session (liveness = rollout mtime)."""
    session_dir = store.session_dir(session_id)
    # A stale rollout mtime is never proof that another process is inactive.
    # Durable run ownership is the canonical liveness fact available here.
    run_lease_path = session_dir / "run_leases.json"
    if run_lease_path.is_file() and RunLeaseStore(run_lease_path).active_leases():
        return
    # Liveness = rollout mtime; fall back to the directory's own mtime for a
    # session dir with no (yet) rollout, and skip if neither is stat-able.
    age = _age_days(store.rollout_path(session_id), now)
    if age is None:
        age = _age_days(session_dir, now)
    if age is None:
        return

    if session_ttl_days > 0 and age > session_ttl_days:
        try:
            released, reclaimed = _prune_expired_session_artifacts(
                store.root,
                session_dir,
                session_id,
                mutation_guard,
            )
        except Exception as exc:
            logger.warning(
                "workspace cleanup: preserving session after Artifact "
                f"retention pruning failed for {session_id}: {exc}"
            )
            return
        stats.artifact_revisions_released += released
        stats.artifact_blobs_reclaimed += reclaimed
        if mutation_guard is not None:
            mutation_guard()
        if disk_io.remove_tree(session_dir):
            stats.sessions_removed += 1
        return

    if artifact_ttl_days > 0 and age > artifact_ttl_days:
        removed_any = False
        for kind in _ARTIFACT_TIER_KINDS:
            if mutation_guard is not None:
                mutation_guard()
            if disk_io.remove_tree(store.space(session_id, kind)):
                removed_any = True
        if removed_any:
            stats.artifact_dirs_removed += 1


def sweep_workspace(
    store: SessionWorkspace,
    *,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    legal_hold_session_ids: frozenset[str] = frozenset(),
    now: Optional[float] = None,
    mutation_guard: Callable[[], None] | None = None,
) -> CleanupStats:
    """Sweep the whole workspace once and return what was reclaimed.

    Args:
        store: The workspace layout owner to sweep.
        session_ttl_days: Days of rollout inactivity before an unprotected
            session dir is removed whole; ``<= 0`` never expires a session.
        artifact_ttl_days: Days of rollout inactivity before a still-alive
            session sheds its overflow artifacts; ``<= 0`` never sheds them.
        exclude_session_id: A session to leave untouched (the live/resumed one).
        now: Wall-clock seconds override (for tests); defaults to ``time.time()``.
    """
    now = time.time() if now is None else now
    stats = CleanupStats()
    for session_id in store.iter_session_ids():
        if session_id == exclude_session_id or session_id in legal_hold_session_ids:
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
                mutation_guard=mutation_guard,
            )
        except OSError as exc:
            logger.warning(f"workspace cleanup: failed to sweep session {session_id}: {exc}")
    return stats


async def sweep_workspace_async(
    store: SessionWorkspace,
    *,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    legal_hold_session_ids: frozenset[str] = frozenset(),
    now: Optional[float] = None,
    mutation_guard: Callable[[], None] | None = None,
) -> CleanupStats:
    """Cooperative, cancellation-safe runtime counterpart of ``sweep_workspace``."""
    now = time.time() if now is None else now
    stats = CleanupStats()
    for index, session_id in enumerate(store.iter_session_ids(), start=1):
        if session_id != exclude_session_id and session_id not in legal_hold_session_ids:
            stats.scanned += 1
            try:
                _sweep_session(
                    store,
                    session_id,
                    session_ttl_days=session_ttl_days,
                    artifact_ttl_days=artifact_ttl_days,
                    now=now,
                    stats=stats,
                    mutation_guard=mutation_guard,
                )
            except OSError as exc:
                logger.warning(f"workspace cleanup: failed to sweep session {session_id}: {exc}")
        if index % 32 == 0:
            await asyncio.sleep(0)
    return stats


def _stamp_path(store: SessionWorkspace) -> Path:
    return store.root / _WORKSPACE_CLEANUP_STAMP


def run_cleanup_if_due(
    store: SessionWorkspace,
    *,
    enabled: bool,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    legal_hold_session_ids: frozenset[str] = frozenset(),
    now: Optional[float] = None,
    mutation_guard: Callable[[], None] | None = None,
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
        if mutation_guard is not None:
            mutation_guard()
        disk_io.atomic_write(stamp, b"", fsync=False)
    except OSError as exc:
        # Can't record the stamp — run anyway (worst case: it runs again sooner).
        logger.debug(f"workspace cleanup: could not write stamp {stamp}: {exc}")

    stats = sweep_workspace(
        store,
        session_ttl_days=session_ttl_days,
        artifact_ttl_days=artifact_ttl_days,
        exclude_session_id=exclude_session_id,
        legal_hold_session_ids=legal_hold_session_ids,
        now=now,
        mutation_guard=mutation_guard,
    )
    logger.debug(
        "workspace cleanup: scanned={} sessions_removed={} artifact_dirs_removed={} "
        "artifact_revisions_released={} artifact_blobs_reclaimed={}".format(
            stats.scanned,
            stats.sessions_removed,
            stats.artifact_dirs_removed,
            stats.artifact_revisions_released,
            stats.artifact_blobs_reclaimed,
        )
    )
    return stats


async def run_cleanup_if_due_async(
    store: SessionWorkspace,
    *,
    enabled: bool,
    session_ttl_days: int,
    artifact_ttl_days: int,
    exclude_session_id: str = "",
    legal_hold_session_ids: frozenset[str] = frozenset(),
    now: Optional[float] = None,
    mutation_guard: Callable[[], None] | None = None,
) -> Optional[CleanupStats]:
    """Cancellation-safe runtime cleanup with the same throttle contract."""
    if not enabled:
        return None
    now = time.time() if now is None else now
    stamp = _stamp_path(store)
    last = disk_io.mtime_seconds(stamp)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        return None
    try:
        if mutation_guard is not None:
            mutation_guard()
        disk_io.atomic_write(stamp, b"", fsync=False)
    except OSError as exc:
        logger.debug(f"workspace cleanup: could not write stamp {stamp}: {exc}")
    return await sweep_workspace_async(
        store,
        session_ttl_days=session_ttl_days,
        artifact_ttl_days=artifact_ttl_days,
        exclude_session_id=exclude_session_id,
        legal_hold_session_ids=legal_hold_session_ids,
        now=now,
        mutation_guard=mutation_guard,
    )


__all__ = [
    "CleanupStats",
    "run_cleanup_if_due",
    "run_cleanup_if_due_async",
    "sweep_workspace",
    "sweep_workspace_async",
]
