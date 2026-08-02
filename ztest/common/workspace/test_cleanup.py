#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.runtime.session.workspace.cleanup`.

Covers the two-tier session-unit TTL (whole-session vs artifact-only expiry),
the "never clean" (``<= 0``) form, current-session exclusion, and the 24h
throttle / disabled short-circuits of
:func:`run_cleanup_if_due`.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.run_lease import RunLeaseStore
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace
from mote.runtime.session.workspace.cleanup import run_cleanup_if_due, sweep_workspace

_DAY = 86_400.0


def _make_session(
    store: SessionWorkspace,
    session_id: str,
    *,
    age_days: float,
    now: float,
    current_rollout: bool = False,
) -> None:
    """Create a session dir (rollout + both overflow spaces), aged.

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
    for kind in SessionSpace:
        space = store.space(session_id, kind)
        space.mkdir(parents=True, exist_ok=True)
        (space / "f").write_text("data")
    stamp = now - age_days * _DAY
    os.utime(rollout, (stamp, stamp))


class TestSweepWorkspace:
    def test_dead_session_removed_whole(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "dead", age_days=40, now=now)
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert not store.session_dir("dead").exists()
        assert stats.sessions_removed == 1
        assert stats.artifact_dirs_removed == 0

    def test_stale_alive_session_sheds_only_artifacts(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "stale", age_days=10, now=now)  # > 7d artifact, <= 30d session
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        # The rollout survives; bulky overflow artifacts are gone.
        assert store.rollout_path("stale").exists()
        assert not store.space("stale", SessionSpace.TOOL_RESULTS).exists()
        assert not store.space("stale", SessionSpace.TASK_OUTPUTS).exists()
        assert stats.sessions_removed == 0
        assert stats.artifact_dirs_removed == 1

    def test_fresh_session_untouched(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "fresh", age_days=1, now=now)
        sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert store.rollout_path("fresh").exists()
        assert store.space("fresh", SessionSpace.TOOL_RESULTS).exists()

    def test_zero_session_ttl_never_expires_session(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "ancient", age_days=1000, now=now)
        # session_ttl=0 -> never remove whole session; artifact tier still applies.
        sweep_workspace(store, session_ttl_days=0, artifact_ttl_days=7, now=now)
        assert store.rollout_path("ancient").exists()
        assert not store.space("ancient", SessionSpace.TOOL_RESULTS).exists()

    def test_zero_artifact_ttl_never_sheds_artifacts(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "keep", age_days=10, now=now)
        # Both tiers off for the artifact tier -> nothing pruned below session TTL.
        sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=0, now=now)
        assert store.space("keep", SessionSpace.TOOL_RESULTS).exists()

    def test_current_session_excluded(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
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
        assert store.space("live", SessionSpace.TOOL_RESULTS).exists()
        assert stats.scanned == 0

    def test_other_process_live_run_lease_overrides_stale_mtime(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "live-elsewhere", age_days=1000, now=now)
        leases = RunLeaseStore(store.session_dir("live-elsewhere") / "run_leases.json")
        leases.acquire("turn", "other-process", 60)
        sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert store.rollout_path("live-elsewhere").exists()
        assert store.space("live-elsewhere", SessionSpace.TOOL_RESULTS).exists()

    def test_orphan_session_dir_uses_dir_mtime(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        # A session dir with no rollout falls back to its own mtime.
        sess = store.session_dir("orphan")
        sess.mkdir(parents=True)
        stamp = now - 40 * _DAY
        os.utime(sess, (stamp, stamp))
        stats = sweep_workspace(store, session_ttl_days=30, artifact_ttl_days=7, now=now)
        assert not sess.exists()
        assert stats.sessions_removed == 1


class TestRunCleanupIfDue:
    def test_disabled_is_noop(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
        _make_session(store, "dead", age_days=1000, now=now)
        assert run_cleanup_if_due(store, enabled=False, session_ttl_days=30, artifact_ttl_days=7, now=now) is None
        assert store.session_dir("dead").exists()

    def test_first_run_sweeps_then_throttled(self, tmp_path):
        now = time.time()
        store = SessionWorkspace(tmp_path)
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
        store = SessionWorkspace(tmp_path)
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
