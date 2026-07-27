from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mote.runtime.agent.runtime_maintenance import RuntimeMaintenance
from mote.runtime.maintenance import MaintenanceCoordinator


def test_maintenance_coordinator_isolated_scopes_and_release():
    first = MaintenanceCoordinator()
    second = MaintenanceCoordinator()

    assert first.acquire_repo_scan("repo") is True
    assert first.acquire_repo_scan("repo") is False
    assert second.acquire_repo_scan("repo") is True
    first.release_repo_scan("repo")
    assert first.acquire_repo_scan("repo") is True

    assert first.acquire_workspace_cleanup() is True
    assert first.acquire_workspace_cleanup() is False
    assert second.acquire_workspace_cleanup() is True
    first.release_workspace_cleanup()
    assert first.acquire_workspace_cleanup() is True


@pytest.mark.asyncio
async def test_repo_cold_scan_does_not_block_startup_and_is_cancelled_on_close():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Indexer:
        async def scan_all_async(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    indexer = Indexer()
    role = SimpleNamespace(
        state=SimpleNamespace(session_id="session-1", project_root="/tmp/mote-maintenance-test"),
        get_cwd=lambda: "/tmp/mote-maintenance-test",
    )
    maintenance = RuntimeMaintenance(role, get=lambda name: indexer, peek=lambda name: None)

    await maintenance.kickoff_repo_scan()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await maintenance.close()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_close_releases_repo_lease_when_task_never_started():
    coordinator = MaintenanceCoordinator()

    class Indexer:
        async def scan_all_async(self):
            await asyncio.Event().wait()

    role = SimpleNamespace(
        state=SimpleNamespace(session_id="session-1", project_root="/tmp/mote-maintenance-cancel"),
        get_cwd=lambda: "/tmp/mote-maintenance-cancel",
    )
    first = RuntimeMaintenance(
        role,
        get=lambda name: Indexer(),
        peek=lambda name: None,
        coordinator=coordinator,
    )
    await first.kickoff_repo_scan()
    await first.close()

    assert coordinator.acquire_repo_scan("/tmp/mote-maintenance-cancel") is True
