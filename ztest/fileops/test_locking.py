from __future__ import annotations

import multiprocessing
import threading

import pytest

from mote.contracts.file import FileLockCancelledError, FileLockTimeoutError, LockMode, LockSpec
from mote.runtime.fileops.locking import NAME_LOCK_LEVEL, PROJECT_LOCK_LEVEL, HierarchicalLockManager


def _hold_lock(root: str, spec: LockSpec, ready, release) -> None:
    manager = HierarchicalLockManager(root)
    with manager.acquire_many([spec], timeout=5):
        ready.set()
        release.wait(5)


def _context():
    return multiprocessing.get_context("spawn")


def _spawn_holder(tmp_path, spec):
    ctx = _context()
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(str(tmp_path / "locks"), spec, ready, release))
    process.start()
    assert ready.wait(15)
    return process, release


def test_exclusive_lock_contends_across_processes(tmp_path):
    spec = LockSpec(PROJECT_LOCK_LEVEL, "project", LockMode.EXCLUSIVE, "project")
    process, release = _spawn_holder(tmp_path, spec)
    manager = HierarchicalLockManager(tmp_path / "locks")
    try:
        with pytest.raises(FileLockTimeoutError):
            with manager.acquire_many([spec], timeout=0.15):
                pass
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_project_shared_locks_are_compatible_but_exclusive_waits(tmp_path):
    shared = LockSpec(PROJECT_LOCK_LEVEL, "project", LockMode.SHARED, "project")
    exclusive = LockSpec(PROJECT_LOCK_LEVEL, "project", LockMode.EXCLUSIVE, "project")
    process, release = _spawn_holder(tmp_path, shared)
    manager = HierarchicalLockManager(tmp_path / "locks")
    try:
        with manager.acquire_many([shared], timeout=0.5):
            pass
        with pytest.raises(FileLockTimeoutError):
            with manager.acquire_many([exclusive], timeout=0.15):
                pass
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_process_crash_releases_kernel_lock(tmp_path):
    spec = LockSpec(NAME_LOCK_LEVEL, "name", LockMode.EXCLUSIVE, "name")
    process, _ = _spawn_holder(tmp_path, spec)
    process.terminate()
    process.join(5)
    manager = HierarchicalLockManager(tmp_path / "locks")

    with manager.acquire_many([spec], timeout=1):
        pass


def test_lock_set_is_sorted_and_reentrant(tmp_path):
    manager = HierarchicalLockManager(tmp_path / "locks")
    project = LockSpec(PROJECT_LOCK_LEVEL, "z-project", LockMode.SHARED, "project")
    name = LockSpec(NAME_LOCK_LEVEL, "a-name", LockMode.EXCLUSIVE, "name")

    with manager.acquire_many([name, project], timeout=1):
        with manager.acquire_many([project, name], timeout=1):
            pass


def test_lock_wait_can_be_cancelled(tmp_path):
    spec = LockSpec(NAME_LOCK_LEVEL, "name", LockMode.EXCLUSIVE, "name")
    process, release = _spawn_holder(tmp_path, spec)
    manager = HierarchicalLockManager(tmp_path / "locks")
    cancel = threading.Event()
    cancel.set()

    try:
        with pytest.raises(FileLockCancelledError, match="cancelled"):
            with manager.acquire_many([spec], timeout=1, cancel=cancel):
                pass
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0
