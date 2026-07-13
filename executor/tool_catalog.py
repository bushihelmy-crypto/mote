#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ToolCatalog — the executor's bound-tool store and schema view.

Owns the single ``name -> BaseTool instance`` map every dispatch path resolves
against, and all the *derived* reads over it: alias-deduped iteration, identity
grouping (every name routing to one instance), category classification
(builtin / mcp / pipeline), the per-category schema collections, the native
tool-use specs, and the reconstructable-tool-name set.

Split out of :class:`~mote.executor.tool_executor.ToolExecutor` so the executor
keeps one job — dispatching a call through the control plane — while the
catalog owns "what tools exist and how they describe themselves". The map is
the one source of truth; MCP hot-reload and per-tool deregistration mutate it
through :meth:`register` / :meth:`remove`, and every schema getter is a pure
read derived from it.
"""
from __future__ import annotations

from typing import Any, Iterator

from mote.executor.mcp_adapter import MCPToolAdapter
from mote.executor.tasks.bggraph.marker import is_pipeline_tool
from mote.executor.tool_spec_adapter import to_native_tool_specs


class ToolCatalog:
    """Store + schema view for an executor's bound tools (static + dynamic)."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}  # name -> BaseTool instance (aliases share one instance)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @property
    def tools(self) -> dict[str, Any]:
        """The live name→instance map (read access for introspection/compat)."""
        return self._tools

    def register(self, tool: Any, names: list[str]) -> None:
        """Bind *tool* under every name in *names* (primary + aliases)."""
        for name in names:
            self._tools[name] = tool

    def get(self, name: str) -> Any | None:
        """Resolve a tool by name/alias, or None."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """All bound names (aliases included)."""
        return list(self._tools.keys())

    def names_for(self, tool: Any) -> list[str]:
        """Every name routing to the SAME instance (by identity, not equality).

        So removing a tool takes all its aliases together and leaves names that
        point at *other* instances untouched.
        """
        return [n for n, t in self._tools.items() if t is tool]

    def remove(self, names: list[str]) -> None:
        """Drop the given names from the map."""
        for name in names:
            del self._tools[name]

    def clear(self) -> None:
        """Forget every bound tool."""
        self._tools.clear()

    def iter_unique(self) -> Iterator[Any]:
        """Yield each bound instance exactly once, deduping aliases by identity.

        The single dedup primitive behind every schema collection and the
        cleanup sweep — so the "seen ids" bookkeeping lives in one place.
        """
        seen: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            yield tool

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _is_mcp_tool(self, tool: Any) -> bool:
        """Return True if the tool is a runtime-discovered MCP adapter."""
        return isinstance(tool, MCPToolAdapter)

    def _is_pipeline_tool(self, tool: Any) -> bool:
        """Return True if the tool is backed by a compiled BgGraph pipeline."""
        return is_pipeline_tool(tool)

    def category(self, tool: Any) -> str:
        """Classify a tool as ``mcp`` / ``pipeline`` / ``builtin``.

        MCP adapters and pipeline tools are runtime/graph-backed and get their
        own system-prompt sections; everything else is a built-in command. MCP
        is checked first since an MCP adapter never wires a compiled graph.
        """
        if self._is_mcp_tool(tool):
            return "mcp"
        if self._is_pipeline_tool(tool):
            return "pipeline"
        return "builtin"

    def mcp_names(self) -> list[str]:
        """All names (aliases included) routing to an MCP-category tool."""
        return [n for n, t in self._tools.items() if self.category(t) == "mcp"]

    # ------------------------------------------------------------------
    # Schema views (pure reads)
    # ------------------------------------------------------------------

    def schemas_for(self, category: str | None) -> dict[str, dict]:
        """Collect deduplicated tool schemas.

        Filters to *category* when given; ``None`` returns every category.
        Aliases collapse onto one entry keyed by the schema's primary name.
        """
        schemas: dict[str, dict] = {}
        for tool in self.iter_unique():
            if category is not None and self.category(tool) != category:
                continue
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def native_specs(self, provider: str = "anthropic") -> list[dict]:
        """Return native tool-use specs for every bound tool.

        Each tool contributes a {name, description, input_schema} record (via
        ``native_schema``), wrapped into the provider envelope. Deduplicated
        like :meth:`schemas_for`.
        """
        native: dict[str, dict] = {}
        for tool in self.iter_unique():
            schema = tool.native_schema()
            native[schema["name"]] = schema
        return to_native_tool_specs(native, provider=provider)

    def reconstructable_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools whose results are re-derivable.

        A tool self-declares this via the ``reconstructable`` ClassVar. Every
        name a tool routes under is included so the Transcript matches whichever
        alias the model used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if getattr(tool, "reconstructable", False):
                names.add(name)
        return frozenset(names)

    def graph_tool_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools that are graph orchestrators.

        A tool self-declares this via the ``is_graph_tool`` ClassVar. run_graph
        uses this to refuse referencing another graph tool from a node (no
        graph-in-graph nesting). Every alias is included so the check matches
        whichever name a spec used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if getattr(tool, "is_graph_tool", False):
                names.add(name)
        return frozenset(names)
