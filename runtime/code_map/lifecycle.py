"""Owner-local lifecycle for advisory repository indexing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from mote.contracts.ports.code_intelligence.code_map import CodeMapIndexer
from mote.runtime.code_map.scan_gate import CodeMapScanGate
from mote.runtime.telemetry.logging import logger


class AdvisoryCodeMapIndexer(Protocol):
    async def scan_all_async(self) -> None: ...

    async def refresh_async(self, changed_paths: list[str]) -> None: ...


class CodeMapLifecycle:
    """Own the single advisory scan task for one repository projection."""

    def __init__(
        self,
        *,
        indexer: Callable[[], CodeMapIndexer | AdvisoryCodeMapIndexer | None],
        repository_root: Callable[[], Path],
        session_identity: str,
        gate: CodeMapScanGate,
    ) -> None:
        self._indexer = indexer
        self._repository_root = repository_root
        self._session_identity = session_identity
        self._gate = gate
        self._task: asyncio.Task[None] | None = None
        self._claim: str | None = None

    async def refresh_changed_path(self, path: str) -> None:
        indexer = self._indexer()
        if indexer is None:
            return
        try:
            await indexer.refresh_async([path])
        except Exception as exc:
            logger.warning(f"code-map refresh failed: {exc}")

    def start_scan(self) -> None:
        indexer = self._indexer()
        if indexer is None or (self._task is not None and not self._task.done()):
            return
        claim = str(self._repository_root().resolve())
        if not self._gate.try_acquire(claim):
            return
        self._claim = claim
        self._task = asyncio.create_task(
            self._scan(indexer, claim),
            name=f"mote-code-map-scan-{self._session_identity[:8]}",
        )

    async def _scan(self, indexer: CodeMapIndexer | AdvisoryCodeMapIndexer, claim: str) -> None:
        try:
            await indexer.scan_all_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"code-map scan failed: {exc}")
        finally:
            self._gate.release(claim)
            if self._claim == claim:
                self._claim = None

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._claim is not None:
            self._gate.release(self._claim)
            self._claim = None


__all__ = ["AdvisoryCodeMapIndexer", "CodeMapLifecycle"]
