"""File-watch integration manifest with narrow lifecycle callbacks."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from mote.common.const.paths import CONFIG_ROOT, MOTE_DIR_NAME, mote_project_files
from mote.common.utils.git_state import find_git_root
from mote.common.watching import FileWatchService
from mote.context.code_map.languages import registered_extensions
from mote.executor.mcp.config_source import MCP_CONFIG_FILE_NAME
from mote.roles.component_graph import ComponentSpec
from mote.roles.runtime_modules.integrations import hook_available

HookHandler = Callable[[object], Awaitable[None]]


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

    hook_runner = ctx.dep("hook_manager")
    if hook_runner is None:
        return None
    seen: set[str] = set()
    deduped_roots = [root for root in roots if not (root in seen or seen.add(root))]
    return FileWatchService(
        hook_runner,
        deduped_roots,
        ignore=config.ignore,
        check_interval=config.check_interval,
        bus=ctx.dep("event_bus"),
    )
