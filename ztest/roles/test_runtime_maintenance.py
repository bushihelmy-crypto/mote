from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mote.runtime.agent.runtime_maintenance import RuntimeMaintenance
from mote.runtime.code_map.scan_gate import CodeMapScanGate
from mote.runtime.session.workspace import WorkspaceCleanupGate


def test_domain_coordinators_isolate_scopes_and_release():
    first = CodeMapScanGate()
    second = CodeMapScanGate()

    assert first.try_acquire("repo") is True
    assert first.try_acquire("repo") is False
    assert second.try_acquire("repo") is True
    first.release("repo")
    assert first.try_acquire("repo") is True

    cleanup = WorkspaceCleanupGate()
    other_cleanup = WorkspaceCleanupGate()
    assert cleanup.try_acquire("workspace") is True
    assert cleanup.try_acquire("workspace") is False
    assert other_cleanup.try_acquire("workspace") is True
    cleanup.release("workspace")
    assert cleanup.try_acquire("workspace") is True


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
    maintenance = RuntimeMaintenance(
        role,
        get_repo_index=lambda: indexer,
        get_workspace_store=lambda: None,
        get_artifact_repository_bundle=lambda: None,
        peek_skill_manager=lambda: None,
        peek_executor=lambda: None,
    )

    await maintenance.kickoff_repo_scan()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await maintenance.close()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_close_releases_repo_lease_when_task_never_started():
    coordinator = CodeMapScanGate()

    class Indexer:
        async def scan_all_async(self):
            await asyncio.Event().wait()

    role = SimpleNamespace(
        state=SimpleNamespace(session_id="session-1", project_root="/tmp/mote-maintenance-cancel"),
        get_cwd=lambda: "/tmp/mote-maintenance-cancel",
    )
    first = RuntimeMaintenance(
        role,
        get_repo_index=lambda: Indexer(),
        get_workspace_store=lambda: None,
        get_artifact_repository_bundle=lambda: None,
        peek_skill_manager=lambda: None,
        peek_executor=lambda: None,
        code_map_scan_gate=coordinator,
    )
    await first.kickoff_repo_scan()
    await first.close()

    assert coordinator.try_acquire("/tmp/mote-maintenance-cancel") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [CodeMapScanGate(), WorkspaceCleanupGate()])
async def test_gate_claim_releases_after_cancellation(gate):
    entered = asyncio.Event()

    async def hold_claim():
        async with gate.claim("scope") as acquired:
            assert acquired is True
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_claim())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.try_acquire("scope") is True
