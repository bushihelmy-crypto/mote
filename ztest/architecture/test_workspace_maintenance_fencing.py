from __future__ import annotations

from pathlib import Path

import pytest

from mote.runtime.session.workspace import WorkspaceCleanupGate

ROOT = Path(__file__).resolve().parents[2]


def test_only_one_cross_process_owner_and_takeover_is_monotonic(tmp_path) -> None:
    now = [10.0]
    workspace = str(tmp_path / "workspace")
    old = WorkspaceCleanupGate(owner_id="old", ttl_seconds=1, clock=lambda: now[0])
    current = WorkspaceCleanupGate(owner_id="current", ttl_seconds=10, clock=lambda: now[0])
    assert old.try_acquire(workspace)
    assert not current.try_acquire(workspace)
    now[0] = 12.0
    assert current.try_acquire(workspace)
    with pytest.raises(Exception):
        old.assert_current(workspace)
    with pytest.raises(Exception):
        old.release(workspace)
    current.assert_current(workspace)
    current.release(workspace)


def test_cleanup_rechecks_fence_before_irreversible_mutations() -> None:
    cleanup = (ROOT / "runtime/session/workspace/cleanup.py").read_text(encoding="utf-8")
    assert cleanup.count("mutation_guard()") >= 5
    maintenance = (ROOT / "runtime/agent/runtime_maintenance.py").read_text(encoding="utf-8")
    assert "mutation_guard=lambda:" in maintenance
    assert "assert_current(cleanup_key)" in maintenance


def test_stamp_is_only_a_hint_and_gc_shares_maintenance_owner() -> None:
    cleanup = (ROOT / "runtime/session/workspace/cleanup.py").read_text(encoding="utf-8")
    gate = (ROOT / "runtime/session/workspace/cleanup_gate.py").read_text(encoding="utf-8")
    maintenance = (ROOT / "runtime/agent/runtime_maintenance.py").read_text(encoding="utf-8")
    assert "FileLeaseCoordinator" in gate
    assert "mtime_seconds(stamp)" in cleanup
    assert "try_acquire(cleanup_key)" in maintenance
    assert "await run_disk_io(self._get_artifact_repository_bundle().collector.collect)" in maintenance
