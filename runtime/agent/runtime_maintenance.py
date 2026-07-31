"""Explicit startup maintenance and hot-reload operations for a Role runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from mote.runtime.code_map.indexer import RepoIndexer
from mote.runtime.code_map.scan_gate import CodeMapScanGate
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.workspace import SessionWorkspace, WorkspaceCleanupGate, run_cleanup_if_due_async
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.tool_executor import ToolExecutor


class ArtifactCollector(Protocol):
    async def collect(self) -> object:
        ...


class ArtifactRepositoryBundleView(Protocol):
    collector: ArtifactCollector


class ReloadableSkillService(Protocol):
    def reload(self) -> bool:
        ...


class RuntimeMaintenance:
    def __init__(
        self,
        role,
        *,
        get_repo_index: Callable[[], RepoIndexer | None],
        get_workspace_store: Callable[[], SessionWorkspace],
        get_artifact_repository_bundle: Callable[[], ArtifactRepositoryBundleView],
        peek_skill_manager: Callable[[], ReloadableSkillService | None],
        peek_executor: Callable[[], ToolExecutor[object] | None],
        code_map_scan_gate: CodeMapScanGate | None = None,
        workspace_cleanup_gate: WorkspaceCleanupGate | None = None,
    ) -> None:
        self._role = role
        self._get_repo_index = get_repo_index
        self._get_workspace_store = get_workspace_store
        self._get_artifact_repository_bundle = get_artifact_repository_bundle
        self._peek_skill_manager = peek_skill_manager
        self._peek_executor = peek_executor
        self._code_map_scan_gate = code_map_scan_gate
        self._workspace_cleanup_gate = workspace_cleanup_gate
        self._owned_code_map_scan_gate: CodeMapScanGate | None = None
        self._owned_workspace_cleanup_gate: WorkspaceCleanupGate | None = None
        self._repo_scan_task: asyncio.Task | None = None
        self._repo_scan_indexer: RepoIndexer | None = None
        self._repo_scan_key: str | None = None
        self._workspace_cleanup_task: asyncio.Task | None = None
        self._workspace_cleanup_key: str | None = None
        self._artifact_gc_task: asyncio.Task | None = None
        self._reconciliation_tasks: dict[str, asyncio.Task[None]] = {}

    def schedule_reconciliation(
        self,
        name: str,
        reconcile: Callable[[], Awaitable[bool]],
    ) -> None:
        """Keep retrying one durable backlog until it is completely drained."""
        existing = self._reconciliation_tasks.get(name)
        if existing is not None and not existing.done():
            return
        self._reconciliation_tasks[name] = asyncio.create_task(
            self._run_reconciliation(name, reconcile),
            name=f"mote-{name}-reconciliation",
        )

    async def _run_reconciliation(
        self,
        name: str,
        reconcile: Callable[[], Awaitable[bool]],
    ) -> None:
        delay = 0.05
        try:
            while True:
                await asyncio.sleep(delay)
                try:
                    if await reconcile():
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"RuntimeMaintenance: {name} reconciliation failed: {exc}")
                delay = min(delay * 2, 5.0)
        finally:
            current = asyncio.current_task()
            if self._reconciliation_tasks.get(name) is current:
                self._reconciliation_tasks.pop(name, None)

    def _repo_coordination(self) -> CodeMapScanGate:
        if self._code_map_scan_gate is not None:
            return self._code_map_scan_gate
        wiring = getattr(self._role, "wiring", None)
        services = getattr(wiring, "services", None)
        inherited = getattr(services, "code_map_scan_gate", None)
        if inherited is not None:
            return inherited
        if self._owned_code_map_scan_gate is None:
            self._owned_code_map_scan_gate = CodeMapScanGate()
        return self._owned_code_map_scan_gate

    def _workspace_coordination(self) -> WorkspaceCleanupGate:
        if self._workspace_cleanup_gate is not None:
            return self._workspace_cleanup_gate
        wiring = getattr(self._role, "wiring", None)
        services = getattr(wiring, "services", None)
        inherited = getattr(services, "workspace_cleanup_gate", None)
        if inherited is not None:
            return inherited
        if self._owned_workspace_cleanup_gate is None:
            self._owned_workspace_cleanup_gate = WorkspaceCleanupGate()
        return self._owned_workspace_cleanup_gate

    async def reindex_code_map_on_change(self, hook_input) -> None:
        indexer = self._get_repo_index()
        if indexer is None:
            return
        payload = getattr(hook_input, "payload", None)
        path = getattr(payload, "path", None)
        if not path:
            return
        try:
            await indexer.refresh_async([path])
        except Exception as exc:  # maintenance is advisory
            logger.warning(f"RuntimeMaintenance: code-map reindex failed: {exc}")

    async def kickoff_repo_scan(self) -> None:
        indexer = self._get_repo_index()
        if indexer is None:
            return
        if self._repo_scan_task is not None and not self._repo_scan_task.done():
            return
        scan_key = str(Path(self._role.state.project_root or self._role.get_cwd()).resolve())
        if not self._repo_coordination().try_acquire(scan_key):
            return
        self._repo_scan_indexer = indexer
        self._repo_scan_key = scan_key
        self._repo_scan_task = asyncio.create_task(
            self._run_repo_scan(),
            name=f"mote-repo-scan-{self._role.state.session_id[:8]}",
        )

    async def _run_repo_scan(self) -> None:
        indexer = self._repo_scan_indexer
        if indexer is None:
            return
        try:
            await indexer.scan_all_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - scan_all is best-effort
            logger.warning(f"RuntimeMaintenance: code-map cold scan failed: {exc}")
        finally:
            if self._repo_scan_key is not None:
                self._repo_coordination().release(self._repo_scan_key)
                self._repo_scan_key = None

    async def kickoff_workspace_cleanup(self) -> None:
        config = self._role.config.workspace.cleanup
        if not config.enabled:
            return
        store = self._get_workspace_store()
        cleanup_key = str(store.root.resolve())
        if not self._workspace_coordination().try_acquire(cleanup_key):
            return
        self._workspace_cleanup_key = cleanup_key
        self._workspace_cleanup_task = asyncio.create_task(
            self._run_workspace_cleanup(store, config),
            name="mote-workspace-cleanup",
        )

    def kickoff_artifact_gc(self) -> None:
        if self._artifact_gc_task is None or self._artifact_gc_task.done():
            collector = self._get_artifact_repository_bundle().collector
            self._artifact_gc_task = asyncio.create_task(
                self._run_artifact_gc(collector),
                name="mote-artifact-gc",
            )

    @staticmethod
    async def _run_artifact_gc(collector) -> None:
        try:
            await run_disk_io(collector.collect)
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: Artifact GC failed: {exc}")

    async def _run_workspace_cleanup(self, store: SessionWorkspace, config) -> None:
        try:
            await run_cleanup_if_due_async(
                store,
                enabled=config.enabled,
                session_ttl_days=config.session_ttl_days,
                artifact_ttl_days=config.artifact_ttl_days,
                exclude_session_id=self._role.state.session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: workspace cleanup failed: {exc}")
        finally:
            if self._workspace_cleanup_key is not None:
                self._workspace_coordination().release(self._workspace_cleanup_key)
                self._workspace_cleanup_key = None

    async def close(self) -> None:
        """Cancel and join all maintenance tasks owned by this Role."""
        tasks = [
            task
            for task in (
                self._repo_scan_task,
                self._workspace_cleanup_task,
                self._artifact_gc_task,
                *self._reconciliation_tasks.values(),
            )
            if task is not None and not task.done()
        ]
        self._reconciliation_tasks.clear()
        self._repo_scan_task = None
        self._workspace_cleanup_task = None
        self._artifact_gc_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._repo_scan_key is not None:
            self._repo_coordination().release(self._repo_scan_key)
            self._repo_scan_key = None
        if self._workspace_cleanup_key is not None:
            self._workspace_coordination().release(self._workspace_cleanup_key)
            self._workspace_cleanup_key = None

    def config_source_roots(self) -> list[str]:
        return [str(path) for path in self._role.wiring.dependencies.watched_config_files]

    async def reload_skills_on_change(self, hook_input) -> None:
        manager = self._peek_skill_manager()
        if manager is not None and manager.reload():
            logger.debug("RuntimeMaintenance: skills hot-reloaded")

    async def reload_config_on_change(self, hook_input) -> None:
        services = self._role.wiring.services
        reloader = services.application_reloader if services is not None else None
        if reloader is not None:
            await reloader.reload()

    async def reload_mcp_on_change(self, hook_input) -> None:
        executor = self._peek_executor()
        if executor is None:
            return
        try:
            enabled = self._role.config.mcp.enabled
            if await executor.reload_mcp(self._role.role_schema.mcps, enabled=enabled):
                logger.debug("RuntimeMaintenance: MCP hot-reloaded")
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: MCP hot-reload failed: {exc}")
