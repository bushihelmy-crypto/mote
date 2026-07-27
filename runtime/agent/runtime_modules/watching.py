"""File-watch integration manifest with narrow lifecycle callbacks."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mote.contracts.fileops.models import FileChangeAttribution, FileVersion, FileVersionTransition
from mote.contracts.ports import FileChangePort
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.runtime_modules.integrations import hook_available
from mote.runtime.context.code_map.languages import registered_extensions
from mote.runtime.paths import CONFIG_ROOT, MOTE_DIR_NAME, mote_project_files
from mote.runtime.tools.mcp.config_source import MCP_CONFIG_FILE_NAME
from mote.runtime.vcs import find_git_root
from mote.runtime.watching import FileWatchService

HookHandler = Callable[[object], Awaitable[None]]


class _DeferredFileChanges:
    """Preserve File Operations laziness behind the watcher's narrow port."""

    def __init__(self, get_file_operations: Callable[[], FileChangePort]) -> None:
        self._get_file_operations = get_file_operations

    def probe_file_version(
        self,
        path: str,
        *,
        prior: Optional[FileVersion] = None,
    ) -> FileVersion:
        return self._get_file_operations().probe_file_version(path, prior=prior)

    def invalidate_external_change(
        self,
        path: str,
        *,
        prior: FileVersion,
        current: FileVersion,
    ) -> None:
        self._get_file_operations().invalidate_external_change(
            path,
            prior=prior,
            current=current,
        )

    def classify_transitions(
        self,
        transitions: tuple[FileVersionTransition, ...],
    ) -> tuple[FileChangeAttribution, ...]:
        return self._get_file_operations().classify_transitions(
            transitions,
        )


@dataclass(frozen=True)
class WatchingCallbacks:
    register_hook: Callable[[str, HookHandler, str | None], None]
    reload_skills: HookHandler
    reload_config: HookHandler
    reload_mcp: HookHandler
    reindex_code_map: HookHandler
    config_source_roots: Callable[[], list[str]]


def watching_component_specs(callbacks: WatchingCallbacks) -> list[ComponentSpec]:
    return [ComponentSpec("file_watch_service", lambda ctx: _build_file_watch_service(ctx, callbacks))]


def _build_file_watch_service(ctx, callbacks: WatchingCallbacks):
    role = ctx.role
    config = role.role_schema.file_watch
    if config is None or not config.enabled:
        return None

    roots = list(config.roots)
    if not roots:
        git_root = find_git_root(role.get_cwd())
        if git_root:
            roots.append(str(git_root))

    if config.reload_skills:
        callbacks.register_hook("FileChanged", callbacks.reload_skills, r"SKILL\.md$")
        roots.extend(ctx.dep("skill_manager").source_dirs())
    if config.reload_config:
        callbacks.register_hook("FileChanged", callbacks.reload_config, r"config2?\.yaml$")
        roots.extend(callbacks.config_source_roots())
    if config.reload_mcp:
        callbacks.register_hook("FileChanged", callbacks.reload_mcp, r"mcp\.json$")
        cwd = Path(role.get_cwd())
        mcp_files = {str(CONFIG_ROOT / MCP_CONFIG_FILE_NAME)}
        mcp_files.update(str(path) for path in mote_project_files(MCP_CONFIG_FILE_NAME, cwd))
        mcp_files.add(str(cwd / MOTE_DIR_NAME / MCP_CONFIG_FILE_NAME))
        roots.extend(sorted(mcp_files))

    if hook_available(role, ctx.state):
        extensions = "|".join(re.escape(ext) for ext in sorted(registered_extensions()))
        callbacks.register_hook(
            "FileChanged",
            callbacks.reindex_code_map,
            rf"({extensions})$",
        )

    seen: set[str] = set()
    deduped_roots = [root for root in roots if not (root in seen or seen.add(root))]
    return FileWatchService(
        deduped_roots,
        file_changes=_DeferredFileChanges(ctx.defer("file_operations")),
        ignore=config.ignore,
        check_interval=config.check_interval,
        telemetry=ctx.dep("telemetry"),
    )
