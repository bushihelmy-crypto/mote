"""Explicit startup maintenance and hot-reload operations for a Role runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from mote.common.config.loader import load_config
from mote.common.config.sources import discover_source_files
from mote.common.logs import logger
from mote.common.workspace import run_cleanup_if_due


class RuntimeMaintenance:
    def __init__(
        self,
        role,
        *,
        get: Callable[[str], object],
        peek: Callable[[str], object | None],
    ) -> None:
        self._role = role
        self._get = get
        self._peek = peek

    async def reindex_code_map_on_change(self, hook_input) -> None:
        indexer = self._peek("repo_index")
        if indexer is None:
            return
        payload = getattr(hook_input, "payload", None)
        path = payload.get("path") if isinstance(payload, dict) else None
        if not path:
            return
        try:
            indexer.refresh([path])
        except Exception as exc:  # maintenance is advisory
            logger.warning(f"RuntimeMaintenance: code-map reindex failed: {exc}")

    async def kickoff_repo_scan(self) -> None:
        indexer = self._get("repo_index")
        if indexer is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, indexer.scan_all)
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: code-map cold scan failed: {exc}")

    async def kickoff_workspace_cleanup(self) -> None:
        config = self._role.config.workspace.cleanup
        if not config.enabled:
            return
        store = self._get("workspace_store")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: run_cleanup_if_due(
                    store,
                    enabled=config.enabled,
                    session_ttl_days=config.session_ttl_days,
                    artifact_ttl_days=config.artifact_ttl_days,
                    exclude_session_id=self._role.state.session_id,
                ),
            )
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: workspace cleanup failed: {exc}")

    def config_source_roots(self) -> list[str]:
        try:
            return [str(source.path) for source in discover_source_files(Path(self._role.get_cwd()))]
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: config source discovery failed: {exc}")
            return []

    async def reload_skills_on_change(self, hook_input) -> None:
        manager = self._peek("skill_manager")
        if manager is not None and manager.reload():
            logger.debug("RuntimeMaintenance: skills hot-reloaded")

    async def reload_config_on_change(self, hook_input) -> None:
        try:
            self._role.config = load_config(Path(self._role.get_cwd()), reload=True)
            logger.debug("RuntimeMaintenance: config hot-reloaded")
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: config hot-reload failed: {exc}")

    async def reload_mcp_on_change(self, hook_input) -> None:
        executor = self._peek("executor")
        if executor is None:
            return
        try:
            enabled = self._role.config.mcp.enabled
            if await executor.reload_mcp(self._role.role_schema.mcps, enabled=enabled):
                logger.debug("RuntimeMaintenance: MCP hot-reloaded")
        except Exception as exc:
            logger.warning(f"RuntimeMaintenance: MCP hot-reload failed: {exc}")
