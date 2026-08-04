from __future__ import annotations

import asyncio

import pytest

from mote.runtime.code_map.lifecycle import CodeMapLifecycle
from mote.runtime.code_map.scan_gate import CodeMapScanGate


@pytest.mark.asyncio
async def test_scan_is_owner_local_and_releases_claim_on_close(tmp_path) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Indexer:
        async def scan_all_async(self) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def refresh_async(self, changed_paths: list[str]) -> None:
            del changed_paths

    gate = CodeMapScanGate()
    lifecycle = CodeMapLifecycle(
        indexer=lambda: Indexer(),
        repository_root=lambda: tmp_path,
        session_identity="session-1",
        gate=gate,
    )
    lifecycle.start_scan()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await lifecycle.close()

    assert cancelled.is_set()
    assert gate.try_acquire(str(tmp_path.resolve())) is True


def test_generic_runtime_maintenance_is_retired() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert not (root / "runtime/agent/runtime_maintenance.py").exists()
