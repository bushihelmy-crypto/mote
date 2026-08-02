"""Product-owned collections of model-facing tools.

Toolsets organize discovery, visibility, and lifecycle.  Permission, effect
ledgering, snapshots, settlement, and audit remain mandatory Runtime concerns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mote.contracts.tool import CommandProtocol
from mote.product.toolsets.builtin.agent_tool import Agent
from mote.product.toolsets.builtin.bash import Bash
from mote.product.toolsets.builtin.cancel_tasks import CancelTasks
from mote.product.toolsets.builtin.canvas import Canvas
from mote.product.toolsets.builtin.device_use import DeviceUse
from mote.product.toolsets.builtin.edit import Edit
from mote.product.toolsets.builtin.generate_media.generate_media_tool import GenerateMedia
from mote.product.toolsets.builtin.human import AskUser, AskUserQuestion, ReplyToUser
from mote.product.toolsets.builtin.python import Python
from mote.product.toolsets.builtin.read import Read
from mote.product.toolsets.builtin.search import Search
from mote.product.toolsets.builtin.search_tools import SearchTools
from mote.product.toolsets.builtin.skill_tool import Skill
from mote.product.toolsets.builtin.sleep import Sleep
from mote.product.toolsets.builtin.terminal import Terminal
from mote.product.toolsets.builtin.web_browser import WebBrowser
from mote.product.toolsets.builtin.web_search import WebSearch
from mote.product.workflows.cancel_run import CancelWorkflowRun
from mote.product.workflows.run_graph.get_node_state import GetNodeState
from mote.product.workflows.run_graph.resume_tasks import ResumeTasks
from mote.product.workflows.run_graph.tool import RunGraph
from mote.runtime.tools.provider import AnyToolset
from mote.runtime.tools.tool_registry import NativeCatalogToolset, ToolCatalog, XmlCatalogToolset

_BUILTIN_TOOL_TYPES = (
    Agent,
    AskUser,
    AskUserQuestion,
    Bash,
    CancelTasks,
    Canvas,
    DeviceUse,
    Edit,
    GenerateMedia,
    Python,
    Read,
    ReplyToUser,
    RunGraph,
    ResumeTasks,
    GetNodeState,
    CancelWorkflowRun,
    Search,
    SearchTools,
    Skill,
    Sleep,
    Terminal,
    WebBrowser,
    WebSearch,
)


def builtin_tool_catalog() -> ToolCatalog:
    """Freeze bundled tool declarations into an Application-owned snapshot."""

    assert RunGraph.name == "RunGraph"
    builtin_names = set().union(*BUILTIN_TOOL_GROUPS.values())
    return ToolCatalog.from_types(tool_type for tool_type in _BUILTIN_TOOL_TYPES if tool_type.name in builtin_names)


WORKSPACE_TOOLS = frozenset({"Read", "Edit", "Search"})
EXECUTION_TOOLS = frozenset({"Bash", "Terminal", "Jupyter"})
WEB_TOOLS = frozenset({"WebSearch", "WebBrowser", "DeviceUse"})
HUMAN_TOOLS = frozenset({"Ask", "AskUserQuestion", "Reply", "End"})
WORKFLOW_TOOLS = frozenset({"RunGraph", "ResumeTasks", "GetNodeStates", "CancelWorkflowRun"})
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
    exposing a disjoint ownership view. Durable Workflow inspection, resume and
    cancellation resolve their current-Agent capability from the active turn.
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
]
