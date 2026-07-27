#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for SchedulerLock — O_EXCL single-writer lease with stale recovery."""

import json
import os

from mote.orchestration.environment.scheduling.lock import SchedulerLock


def test_acquire_creates_lock_file(tmp_path):
    lock = SchedulerLock("sess-a", base_dir=str(tmp_path))
    assert lock.acquire() is True
    assert lock.is_held is True
    assert lock.path.exists()
    data = json.loads(lock.path.read_text())
    assert data["session_id"] == "sess-a"
    assert data["pid"] == os.getpid()


def test_release_removes_lock(tmp_path):
    lock = SchedulerLock("sess-a", base_dir=str(tmp_path))
    lock.acquire()
    lock.release()
    assert lock.is_held is False
    assert not lock.path.exists()


def test_exclusive_blocks_second_live_session(tmp_path):
    a = SchedulerLock("sess-a", base_dir=str(tmp_path))
    b = SchedulerLock("sess-b", base_dir=str(tmp_path))
    assert a.acquire() is True
    # b sees a live PID (this process) → blocked.
    assert b.acquire() is False
    assert b.is_held is False


def test_idempotent_reacquire_same_session(tmp_path):
    a = SchedulerLock("sess-a", base_dir=str(tmp_path))
    assert a.acquire() is True
    assert a.acquire() is True  # idempotent


def test_stale_pid_recovery(tmp_path):
    # Write a lock owned by a dead PID.
    lock_dir = tmp_path
    lock_dir.mkdir(parents=True, exist_ok=True)
    stale = SchedulerLock("dead-sess", base_dir=str(tmp_path))
    dead_pid = 2_000_000_000  # almost certainly not running
    stale.path.write_text(json.dumps({"session_id": "dead-sess", "pid": dead_pid, "acquired_at": 0}))

    fresh = SchedulerLock("sess-b", base_dir=str(tmp_path))
    assert fresh.acquire() is True
    data = json.loads(fresh.path.read_text())
    assert data["session_id"] == "sess-b"
    assert data["pid"] == os.getpid()


def test_corrupt_lock_treated_as_stale(tmp_path):
    bad = SchedulerLock("x", base_dir=str(tmp_path))
    bad.path.parent.mkdir(parents=True, exist_ok=True)
    bad.path.write_text("not json")
    fresh = SchedulerLock("sess-b", base_dir=str(tmp_path))
    assert fresh.acquire() is True
