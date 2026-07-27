"""Product-owned collections of model-facing tools.

Toolsets organize discovery, visibility, and lifecycle.  Permission, effect
ledgering, snapshots, settlement, and audit remain mandatory Runtime concerns.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from typing import Any

from mote.contracts.tools import CommandProtocol
from mote.kernel.tools.toolset import AnyToolset
from mote.runtime.tools.tool_registry import NativeCatalogToolset, ToolCatalog, XmlCatalogToolset, declared_tool_catalog

_BUILTIN_PACKAGE = ("mote.product.toolsets.builtin",)
_builtin_tools_discovered = False


def discover_builtin_tools() -> None:
    """Import the Product-owned built-in tool modules exactly once."""
    global _builtin_tools_discovered
    if _builtin_tools_discovered:
        return
    _builtin_tools_discovered = True
    for package in _BUILTIN_PACKAGE:
        module = importlib.import_module(package)
        for child in pkgutil.walk_packages(module.__path__, prefix=module.__name__ + "."):
            importlib.import_module(child.name)


def builtin_tool_catalog() -> ToolCatalog:
    """Freeze bundled tool declarations into an Application-owned snapshot."""

    discover_builtin_tools()
    declarations = declared_tool_catalog()
    builtin_names = set().union(*BUILTIN_TOOL_GROUPS.values())
    return ToolCatalog.from_types(
        tool_type for name, tool_type in declarations.all_tools().items() if name in builtin_names
    )


WORKSPACE_TOOLS = frozenset({"Read", "Edit", "Search"})
EXECUTION_TOOLS = frozenset({"Bash", "Terminal", "Jupyter"})
WEB_TOOLS = frozenset({"WebSearch", "WebBrowser", "DeviceUse"})
HUMAN_TOOLS = frozenset({"Ask", "AskUserQuestion", "Reply", "End"})
WORKFLOW_TOOLS = frozenset({"RunGraph"})
ORCHESTRATION_TOOLS = frozenset({"Agent", "CancelTasks", "Sleep"})
EXTENSION_TOOLS = frozenset({"Skill", "SearchTools"})
MEDIA_TOOLS = frozenset({"GenerateMedia"})
INTERACTIVE_TOOLS = frozenset({"Canvas"})

BUILTIN_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "mote.workspace.v1": WORKSPACE_TOOLS,
    "mote.execution.v1": EXECUTION_TOOLS,
    "mote.web.v1": WEB_TOOLS,
    "mote.human.v1": HUMAN_TOOLS,
    "mote.workflow.v1": WORKFLOW_TOOLS,
    "mote.orchestration.v1": ORCHESTRATION_TOOLS,
    "mote.extension.v1": EXTENSION_TOOLS,
    "mote.media.v1": MEDIA_TOOLS,
    "mote.interactive.v1": INTERACTIVE_TOOLS,
}


def builtin_toolsets(
    protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    *,
    catalog: ToolCatalog | None = None,
    capability_factories: Mapping[str, Callable[[], Any]] | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> tuple[AnyToolset, ...]:
    """Return the standard, mutually exclusive Product Toolsets.

    Every returned adapter reads the same immutable Application snapshot while
    exposing a disjoint ownership view. `ResumeTasks` and `GetNodeStates` are deliberately absent:
    their old source files remain only until the underlying pause/resume protocol
    and persisted data are removed as one coherent migration.
    """

    resolved = CommandProtocol(protocol)
    resolved_catalog = catalog or builtin_tool_catalog()
    toolset_type = XmlCatalogToolset if resolved is CommandProtocol.XML else NativeCatalogToolset
    groups = dict(BUILTIN_TOOL_GROUPS)
    plugin_names = frozenset(resolved_catalog.all_tools()).difference(set().union(*groups.values()))
    if plugin_names:
        groups["mote.plugins.v1"] = plugin_names
    return tuple(
        toolset_type(
            id=toolset_id,
            catalog=resolved_catalog,
            include=names,
            capability_factories=capability_factories,
            descriptions=descriptions,
        )
        for toolset_id, names in groups.items()
    )


__all__ = [
    "BUILTIN_TOOL_GROUPS",
    "EXECUTION_TOOLS",
    "EXTENSION_TOOLS",
    "HUMAN_TOOLS",
    "MEDIA_TOOLS",
    "INTERACTIVE_TOOLS",
    "ORCHESTRATION_TOOLS",
    "WEB_TOOLS",
    "WORKFLOW_TOOLS",
    "WORKSPACE_TOOLS",
    "builtin_toolsets",
    "builtin_tool_catalog",
    "discover_builtin_tools",
]
