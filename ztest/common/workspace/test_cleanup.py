#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.runtime.workspace.cleanup`.

Covers the two-tier session-unit TTL (whole-session vs artifact-only expiry),
the "never clean" (``<= 0``) form, current-session exclusion, the one-time
legacy sweep, and the 24h throttle / disabled short-circuits of
:func:`run_cleanup_if_due`.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

import pytest

from mote.contracts.artifacts import ArtifactPublishRequest, ArtifactRepresentationInput, ArtifactRetention
from mote.contracts.errors.artifacts import ArtifactNotFoundError
from mote.runtime.artifacts import ArtifactRepositoryBlobStore, ArtifactRepositoryLayout, DurableArtifactStore
from mote.runtime.fileops import FileOperations
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.workspace import ArtifactKind, WorkspaceStore
from mote.runtime.workspace.cleanup import run_cleanup_if_due, sweep_workspace

_DAY = 86_400.0


def _make_session(
    store: WorkspaceStore,
    session_id: str,
    *,
    age_days: float,
    now: float,
    current_rollout: bool = False,
) -> None:
    """Create a session dir (rollout + blobs + both artifact spaces), aged.

    The rollout mtime is set to *age_days* in the past so it drives the sweep.
    """
    store.session_dir(session_id).mkdir(parents=True, exist_ok=True)
    rollout = store.rollout_path(session_id)
    if current_rollout:
        SessionLog(session_id, base_dir=str(store.sessions_root)).commit_offline(
            SessionMetaEvent(session_id=session_id, project_root=str(store.root))
        )
    else:
        rollout.write_text("{}\n")
    for kind in ArtifactKind:
        space = store.space(session_id, kind)
        space.mkdir(parents=True, exist_ok=True)
        (space / "f").write_text("data")
    stamp = now - age_days * _DAY
    os.utime(rollout, (stamp, stamp))


class TestSweepWorkspace:
    def test_dead_session_removed_whole(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "dead", age_days=40, now=now)
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert not store.session_dir("dead").exists()
        assert stats.sessions_removed == 1
        assert stats.artifact_dirs_removed == 0

    def test_unmigratable_legacy_project_artifact_preserves_session(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "project", age_days=40, now=now)
        index = store.session_dir("project") / "artifacts.sqlite3"
        with sqlite3.connect(index) as connection:
            connection.execute("CREATE TABLE artifact_representations (retention TEXT NOT NULL)")
            connection.execute("CREATE TABLE artifact_publication_outbox (retention TEXT NOT NULL)")
            connection.execute("INSERT INTO artifact_representations VALUES ('project')")

        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)

        assert store.session_dir("project").exists()
        assert stats.sessions_removed == 0
        assert stats.artifact_migration_failures == 1

    def test_durable_artifact_does_not_retain_session_scoped_artifacts(self, tmp_path):
        now = time.time()
        workspace = WorkspaceStore(tmp_path)
        _make_session(
            workspace,
            "mixed",
            age_days=40,
            now=now,
            current_rollout=True,
        )
        session_dir = workspace.session_dir("mixed")
        operations = FileOperations(
            session_id="mixed",
            journal_path=workspace.rollout_path("mixed"),
            get_project_root=lambda: str(tmp_path),
            lock_root=session_dir / "file-locks",
        )
        artifacts = DurableArtifactStore(
            session_dir / "artifacts.sqlite3",
            ArtifactRepositoryBlobStore(operations.artifacts),
        )

        async def publish():
            session = await artifacts.publish(
                ArtifactPublishRequest(
                    artifact_id="session-output",
                    retention=ArtifactRetention.SESSION,
                    representations=(
                        ArtifactRepresentationInput(
                            representation="text",
                            kind="report",
                            mime_type="text/plain",
                            content=b"temporary",
                        ),
                    ),
                )
            )
            project = await artifacts.publish(
                ArtifactPublishRequest(
                    artifact_id="project-output",
                    retention=ArtifactRetention.PROJECT,
                    representations=(
                        ArtifactRepresentationInput(
                            representation="text",
                            kind="report",
                            mime_type="text/plain",
                            content=b"durable",
                        ),
                    ),
                )
            )
            return session, project

        session_revision, project_revision = asyncio.run(publish())
        stats = sweep_workspace(workspace, session_ttl_days=30, artifact_ttl_days=7, now=now)

        assert stats.artifact_revisions_migrated == 2
        assert stats.sessions_removed == 1
        assert not session_dir.exists()
        with pytest.raises(ArtifactNotFoundError):
            asyncio.run(
                artifacts.get_revision(
                    session_revision.artifact_id,
                    session_revision.revision,
                )
            )
        with pytest.raises(ArtifactNotFoundError):
            asyncio.run(
                artifacts.get_revision(
                    project_revision.artifact_id,
                    project_revision.revision,
                )
            )
        layout = ArtifactRepositoryLayout(tmp_path)
        project_store = layout.open(layout.ownership(session_id="next", project_root=tmp_path)).store
        assert asyncio.run(project_store.read(project_revision.get("text"))) == b"durable"

    def test_unmigratable_pending_publication_preserves_session(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "pending", age_days=40, now=now)
        index = store.session_dir("pending") / "artifacts.sqlite3"
        with sqlite3.connect(index) as connection:
            connection.execute("CREATE TABLE artifact_representations (retention TEXT NOT NULL)")
            connection.execute("CREATE TABLE artifact_publication_outbox (retention TEXT NOT NULL)")
            connection.execute("INSERT INTO artifact_publication_outbox VALUES ('pinned')")

        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)

        assert store.session_dir("pending").exists()
        assert stats.artifact_migration_failures == 1

    def test_corrupt_artifact_index_fails_closed_during_cleanup(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "corrupt", age_days=40, now=now)
        (store.session_dir("corrupt") / "artifacts.sqlite3").write_bytes(b"not sqlite")

        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)

        assert store.session_dir("corrupt").exists()
        assert stats.artifact_migration_failures == 1

    def test_stale_alive_session_sheds_only_artifacts(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "stale", age_days=10, now=now)  # > 7d artifact, <= 30d session
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        # Record (rollout + blobs) survives; bulky overflow artifacts are gone.
        assert store.rollout_path("stale").exists()
        assert store.space("stale", ArtifactKind.BLOBS).exists()
        assert not store.space("stale", ArtifactKind.TOOL_RESULTS).exists()
        assert not store.space("stale", ArtifactKind.TASK_OUTPUTS).exists()
        assert stats.sessions_removed == 0
        assert stats.artifact_dirs_removed == 1

    def test_fresh_session_untouched(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "fresh", age_days=1, now=now)
        sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert store.rollout_path("fresh").exists()
        assert store.space("fresh", ArtifactKind.TOOL_RESULTS).exists()

    def test_zero_session_ttl_never_expires_session(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "ancient", age_days=1000, now=now)
        # session_ttl=0 -> never remove whole session; artifact tier still applies.
        sweep_workspace(store, session_ttl_days=0, artifact_ttl_days=7, now=now)
        assert store.rollout_path("ancient").exists()
        assert not store.space("ancient", ArtifactKind.TOOL_RESULTS).exists()

    def test_zero_artifact_ttl_never_sheds_artifacts(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "keep", age_days=10, now=now)
        # Both tiers off for the artifact tier -> nothing pruned below session TTL.
        sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=0, now=now)
        assert store.space("keep", ArtifactKind.TOOL_RESULTS).exists()

    def test_current_session_excluded(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "live", age_days=1000, now=now)
        stats = sweep_workspace(
            store,
            session_ttl_days=30,
            artifact_ttl_days=7,
            exclude_session_id="live",
            now=now,
        )
        # The live/resumed session is never swept out from under itself.
        assert store.rollout_path("live").exists()
        assert store.space("live", ArtifactKind.TOOL_RESULTS).exists()
        assert stats.scanned == 0

    def test_orphan_session_dir_uses_dir_mtime(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        # A session dir with no rollout falls back to its own mtime.
        sess = store.session_dir("orphan")
        sess.mkdir(parents=True)
        stamp = now - 40 * _DAY
        os.utime(sess, (stamp, stamp))
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert not sess.exists()
        assert stats.sessions_removed == 1

    def test_legacy_tree_pruned(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        legacy = tmp_path / ".tool_results"
        old_bucket = legacy / "old_sess"
        old_bucket.mkdir(parents=True)
        (old_bucket / "r.txt").write_text("x")
        stamp = now - 40 * _DAY
        os.utime(old_bucket, (stamp, stamp))
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert not old_bucket.exists()
        # Emptied legacy root is removed too.
        assert not legacy.exists()
        assert stats.legacy_dirs_removed == 1


class TestRunCleanupIfDue:
    def test_disabled_is_noop(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "dead", age_days=1000, now=now)
        assert run_cleanup_if_due(store, enabled=False, session_ttl_days=30, artifact_ttl_days=7, now=now) is None
        assert store.session_dir("dead").exists()

    def test_first_run_sweeps_then_throttled(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "dead", age_days=1000, now=now)
        first = run_cleanup_if_due(store, enabled=True, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert first is not None and first.sessions_removed == 1
        # A second call within 24h is throttled by the stamp file.
        _make_session(store, "dead2", age_days=1000, now=now)
        second = run_cleanup_if_due(
            store,
            enabled=True,
            session_ttl_days=30,
            artifact_ttl_days=7,
            now=now + 3600,
        )
        assert second is None
        assert store.session_dir("dead2").exists()

    def test_runs_again_after_throttle_window(self, tmp_path):
        now = time.time()
        store = WorkspaceStore(tmp_path)
        _make_session(store, "dead", age_days=1000, now=now)
        run_cleanup_if_due(store, enabled=True, session_ttl_days=30, artifact_ttl_days=7, now=now)
        _make_session(store, "dead2", age_days=1000, now=now)
        later = run_cleanup_if_due(
            store,
            enabled=True,
            session_ttl_days=30,
            artifact_ttl_days=7,
            now=now + _DAY + 60,
        )
        assert later is not None and later.sessions_removed == 1
        assert not store.session_dir("dead2").exists()
