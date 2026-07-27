"""Explicit startup maintenance and hot-reload operations for a Role runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from mote.runtime.config.loader import load_config
from mote.runtime.config.sources import discover_source_files
from mote.runtime.context.code_map.indexer import RepoIndexer
from mote.runtime.context.skills.skill_manager import SkillManager
from mote.runtime.fileops.artifact_budgets import ARTIFACT_GC_BATCH_SIZE
from mote.runtime.fileops.gc_service import ArtifactGarbageCollectionService, ArtifactGarbageCollectionTarget
from mote.runtime.logging import logger
from mote.runtime.maintenance import MaintenanceCoordinator
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.workspace import WorkspaceStore, run_cleanup_if_due_async


class RuntimeMaintenance:
    def __init__(
        self,
        role,
        *,
        get: Callable[[str], object],
        peek: Callable[[str], object | None],
        coordinator: MaintenanceCoordinator | None = None,
    ) -> None:
        self._role = role
        self._get = get
        self._peek = peek
        self._coordinator = coordinator
        self._owned_coordinator: MaintenanceCoordinator | None = None
        self._repo_scan_task: asyncio.Task | None = None
        self._repo_scan_indexer: RepoIndexer | None = None
        self._repo_scan_key: str | None = None
        self._workspace_cleanup_task: asyncio.Task | None = None
        self._workspace_cleanup_acquired = False
        self._artifact_gc_service: ArtifactGarbageCollectionService | None = None
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

    def _coordination(self) -> MaintenanceCoordinator:
        if self._coordinator is not None:
            return self._coordinator
        context = getattr(self._role, "_context", None)
        inherited = getattr(context, "maintenance_coordinator", None)
        if inherited is not None:
            return inherited
        if self._owned_coordinator is None:
            self._owned_coordinator = MaintenanceCoordinator()
        return self._owned_coordinator

    async def reindex_code_map_on_change(self, hook_input) -> None:
        indexer = cast(RepoIndexer | None, self._peek("repo_index"))
        if indexer is None:
            return
        payload = getattr(hook_input, "payload", None)
        path = payload.get("path") if isinstance(payload, dict) else None
        if not path:
            return
        try:
            await indexer.refresh_async([path])
        except Exception as exc:  # maintenance is advisory
            logger.warning(f"RuntimeMaintenance: code-map reindex failed: {exc}")

    async def kickoff_repo_scan(self) -> None:
        indexer = cast(RepoIndexer, self._get("repo_index"))
        if indexer is None:
            return
        if self._repo_scan_task is not None and not self._repo_scan_task.done():
            return
        scan_key = str(Path(self._role.state.project_root or self._role.get_cwd()).resolve())
        if not self._coordination().acquire_repo_scan(scan_key):
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
                self._coordination().release_repo_scan(self._repo_scan_key)
                self._repo_scan_key = None

    async def kickoff_workspace_cleanup(self) -> None:
        config = self._role.config.workspace.cleanup
        if not config.enabled:
            return
        store = cast(WorkspaceStore, self._get("workspace_store"))
        if not self._coordination().acquire_workspace_cleanup():
            return
        self._workspace_cleanup_acquired = True
        self._workspace_cleanup_task = asyncio.create_task(
            self._run_workspace_cleanup(store, config),
            name="mote-workspace-cleanup",
        )

    def kickoff_artifact_gc(self) -> None:
        if self._artifact_gc_service is None:
            target = cast(
                ArtifactGarbageCollectionTarget,
                self._get("file_operations"),
            )
            self._artifact_gc_service = ArtifactGarbageCollectionService(
                target,
                batch_size=ARTIFACT_GC_BATCH_SIZE,
            )
        self._artifact_gc_service.start()

    async def _run_workspace_cleanup(self, store: WorkspaceStore, config) -> None:
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
            if self._workspace_cleanup_acquired:
                self._coordination().release_workspace_cleanup()
                self._workspace_cleanup_acquired = False

    async def close(self) -> None:
        """Cancel and join all maintenance tasks owned by this Role."""
        artifact_gc_service, self._artifact_gc_service = (
            self._artifact_gc_service,
            None,
        )
        tasks = [
            task
            for task in (
                self._repo_scan_task,
                self._workspace_cleanup_task,
                *self._reconciliation_tasks.values(),
            )
            if task is not None and not task.done()
        ]
        self._reconciliation_tasks.clear()
        self._repo_scan_task = None
        self._workspace_cleanup_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if artifact_gc_service is not None:
            await artifact_gc_service.aclose()
        if self._repo_scan_key is not None:
            self._coordination().release_repo_scan(self._repo_scan_key)
            self._repo_scan_key = None
        if self._workspace_cleanup_acquired:
            self._coordination().release_workspace_cleanup()
            self._workspace_cleanup_acquired = False

    def config_source_roots(self) -> list[str]:
        try:
            return [str(source.path) for source in discover_source_files(Path(self._role.get_cwd()))]
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: config source discovery failed: {exc}")
            return []

    async def reload_skills_on_change(self, hook_input) -> None:
        manager = cast(SkillManager | None, self._peek("skill_manager"))
        if manager is not None and manager.reload():
            logger.debug("RuntimeMaintenance: skills hot-reloaded")

    async def reload_config_on_change(self, hook_input) -> None:
        try:
            self._role.config = load_config(Path(self._role.get_cwd()), reload=True)
            logger.debug("RuntimeMaintenance: config hot-reloaded")
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: config hot-reload failed: {exc}")

    async def reload_mcp_on_change(self, hook_input) -> None:
        executor = cast(ToolExecutor | None, self._peek("executor"))
        if executor is None:
            return
        try:
            enabled = self._role.config.mcp.enabled
            if await executor.reload_mcp(self._role.role_schema.mcps, enabled=enabled):
                logger.debug("RuntimeMaintenance: MCP hot-reloaded")
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: MCP hot-reload failed: {exc}")
