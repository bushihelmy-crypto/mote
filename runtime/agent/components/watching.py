"""File-watch integration manifest with narrow lifecycle callbacks."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

from mote.contracts.file.identity import FileChangeAttribution, FileVersion, FileVersionTransition
from mote.contracts.ports.file.changes import FileChangePort
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import FILE_OPERATIONS, FILE_WATCH_SERVICE, SKILL_MANAGER, TELEMETRY
from mote.runtime.agent.components.integrations import hook_available
from mote.runtime.code_map.languages import registered_extensions
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
    return [ComponentSpec(FILE_WATCH_SERVICE, lambda ctx: _build_file_watch_service(ctx, callbacks))]


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
        roots.extend(ctx.dep(SKILL_MANAGER).source_dirs())
    if config.reload_config:
        callbacks.register_hook("FileChanged", callbacks.reload_config, r"config2?\.yaml$")
        roots.extend(callbacks.config_source_roots())
    if config.reload_mcp:
        callbacks.register_hook("FileChanged", callbacks.reload_mcp, r"mcp\.json$")
        roots.extend(str(path) for path in role.wiring.dependencies.watched_config_files)

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
        file_changes=_DeferredFileChanges(ctx.defer(FILE_OPERATIONS)),
        ignore=config.ignore,
        check_interval=config.check_interval,
        telemetry=ctx.dep(TELEMETRY),
    )
