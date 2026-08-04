#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ToolCatalog — the executor's bound-tool store and schema view.

Owns the single ``name -> BaseTool instance`` map every dispatch path resolves
against, and all the *derived* reads over it: alias-deduped iteration, identity
grouping (every name routing to one instance), category classification
(builtin / mcp / pipeline), the per-category schema collections, the native
tool-use specs, and the reconstructable-tool-name set.

Split out of :class:`~mote.runtime.tools.tool_executor.ToolExecutor` so the executor
keeps one job — dispatching a call through the control plane — while the
catalog owns "what tools exist and how they describe themselves". The map is
the one source of truth; MCP hot-reload and per-tool deregistration mutate it
through :meth:`register` / :meth:`remove`, and every schema getter is a pure
read derived from it.
"""

from __future__ import annotations

from typing import Callable, Iterator

from mote.kernel.tools.spec_adapter import to_native_tool_specs
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import ExecutableToolBinding


class BoundToolCatalog:
    """Store + schema view for an executor's bound tools (static + dynamic).

    *Deferral* (tool-search): a subset of bound tools can be declared **deferred**
    — their full schema is withheld from both channels' schema views until the
    tool is *revealed* (the model discovers it via the ``SearchTools`` meta-tool).
    Deferral controls schema visibility and execution eligibility. A deferred
    tool remains in the internal ``_tools`` map, but ``ToolExecutor`` refuses a
    call until the tool is revealed. The revealed set is read live via
    ``get_revealed`` (it lives on RoleState so it survives session resume), so
    revelation is durable without depending on compaction preserving history.
    """

    def __init__(
        self,
        deferred: set[str] | None = None,
        get_revealed: Callable[[], set[str]] | None = None,
    ) -> None:
        self._tools: dict[str, ExecutableToolBinding] = {}
        self._generation = 1
        # Names of tools hidden until discovered (schema-visibility only).
        self._deferred: set[str] = set(deferred) if deferred else set()
        # Live getter onto the revealed set (durable, on RoleState). Defaults to
        # an empty set → nothing revealed (a standalone catalog with a deferred
        # set but no owner keeps its deferred tools hidden).
        self._get_revealed: Callable[[], set[str]] = get_revealed or (lambda: set())

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def register(self, tool: ExecutableToolBinding, names: list[str]) -> None:
        """Bind *tool* under every name in *names* (primary + aliases)."""
        duplicates = set(names) & self._tools.keys()
        if duplicates:
            raise ValueError(f"tool namespace conflict: {sorted(duplicates)!r}")
        if len(names) != len(set(names)):
            raise ValueError("tool definition contains duplicate names")
        for name in names:
            self._tools[name] = tool
        self._generation += 1

    def replace_mcp(
        self, bindings: tuple[tuple[ExecutableToolBinding, tuple[str, ...]], ...]
    ) -> tuple[tuple[ExecutableToolBinding, ...], tuple[str, ...]]:
        """Atomically replace the complete MCP category after namespace validation."""
        retained = {name: tool for name, tool in self._tools.items() if self.category(tool) != "mcp"}
        candidate = dict(retained)
        for tool, names in bindings:
            if not names or len(names) != len(set(names)):
                raise ValueError("MCP tool names are empty or duplicated")
            conflicts = set(names) & candidate.keys()
            if conflicts:
                raise ValueError(f"MCP tool namespace conflict: {sorted(conflicts)!r}")
            candidate.update((name, tool) for name in names)
        old_names = tuple(self.mcp_names())
        old_tools_by_identity: dict[int, ExecutableToolBinding] = {}
        for name in old_names:
            tool = self._tools[name]
            old_tools_by_identity.setdefault(id(tool), tool)
        old_tools = tuple(old_tools_by_identity.values())
        self._tools = candidate
        self._generation += 1
        return old_tools, old_names

    def get(self, name: str) -> ExecutableToolBinding | None:
        """Resolve a tool by name/alias, or None."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """All bound names (aliases included)."""
        return list(self._tools.keys())

    def names_for(self, tool: ExecutableToolBinding) -> list[str]:
        """Every name routing to the SAME instance (by identity, not equality).

        So removing a tool takes all its aliases together and leaves names that
        point at *other* instances untouched.
        """
        return [n for n, t in self._tools.items() if t is tool]

    def remove(self, names: list[str]) -> None:
        """Drop the given names from the map."""
        for name in names:
            del self._tools[name]
        self._generation += 1

    def clear(self) -> None:
        """Forget every bound tool."""
        self._tools.clear()
        self._generation += 1

    def iter_unique(self) -> Iterator[ExecutableToolBinding]:
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

    def _is_mcp_tool(self, tool: ExecutableToolBinding) -> bool:
        """Return True if the tool is a runtime-discovered MCP adapter."""
        return isinstance(tool, ExecutableToolBinding) and tool.definition.category == "mcp"

    def _is_pipeline_tool(self, tool: ExecutableToolBinding) -> bool:
        """Return True if the tool is backed by a compiled Workflow pipeline."""
        return tool.definition.execution_kind.is_workflow

    def category(self, tool: ExecutableToolBinding) -> str:
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

    def names_of(self, primary: str) -> frozenset[str]:
        """Every bound name (primary + aliases) routing to the tool whose canonical
        primary name is ``primary`` — empty if no such tool is bound.

        Lets a consumer identify a tool by its ONE stable primary name across all
        the aliases the model may have emitted (``write`` / ``Update`` → ``Edit``),
        without a live per-call resolve. Resolves the primary first (so an alias
        passed as ``primary`` still finds the instance), then returns every name
        pointing at that same instance by identity.
        """
        tool = self._tools.get(primary)
        return frozenset(self.names_for(tool)) if tool is not None else frozenset()

    # ------------------------------------------------------------------
    # Deferral (tool-search visibility)
    # ------------------------------------------------------------------

    def _is_hidden(self, tool: ExecutableToolBinding) -> bool:
        """True when *tool* is deferred and not yet revealed → schema withheld.

        This is the CLIENT-SIDE reveal predicate used by schema projection and
        the executor's dispatch gate.
        """
        name = tool.name
        return name in self._deferred and name not in self._get_revealed()

    def is_hidden(self, name: str) -> bool:
        """Whether *name* resolves to a deferred tool not revealed yet."""
        tool = self._tools.get(name)
        return tool is not None and self._is_hidden(tool)

    def _is_corpus(self, tool: ExecutableToolBinding) -> bool:
        """True when *tool* is a deferred-corpus member (regardless of revealed).

        The wire ``defer_loading`` flag for the Anthropic server-side tool-search
        path is keyed on **corpus membership** — constant across the whole run —
        NOT on the revealed set. So even a revealed tool keeps ``defer_loading``
        on the wire, keeping the ``tools=`` prefix byte-stable (cache preserved).
        """
        return tool.name in self._deferred

    @staticmethod
    def _menu_line(tool: ExecutableToolBinding) -> str:
        """The one-line blurb for *tool* in a search menu.

        The tool's :meth:`~mote.runtime.tools.base_tool.BaseTool.summary` — the first
        line of its ``call()`` docstring, a tight capability-first sentence
        authored for exactly this listing. This is the single seam both menu
        builders share, so the derivation is made in one place. Falls back to
        the tool's name if it exposes no ``summary`` (e.g. a bare stand-in).
        """
        name = tool.name
        return tool.definition.summary or name

    def deferred_index(self, *, include_revealed: bool = True) -> dict[str, str]:
        """The compact menu of deferred tools → ``{name: one-line desc}``.

        Only names + one-line summaries (no parameters) so it stays small. Each
        line is the tool's :meth:`~mote.runtime.tools.base_tool.BaseTool.summary` (see
        :meth:`_menu_line`).

        Two callers with different needs select via *include_revealed*:

          - ``True`` (default) — the whole deferred corpus regardless of the
            revealed set. This is the identity/validation view (e.g.
            :meth:`~mote.runtime.agent.role.Role.reveal_tools` checks a name against it):
            a revealed tool must still be recognised as a deferred name.
          - ``False`` — drop already-revealed tools. This is the DISPLAY view for
            the ephemeral reminder tail (``deferred_tool_index`` source): a
            revealed tool's full schema is already on the active channel, so
            keeping it in the "search to enable" menu is misleading and wastes
            tokens. The menu rides AFTER the cache breakpoint (it is re-injected
            each turn, never in the cached prefix), so shrinking it as tools are
            revealed costs no prompt-cache churn — mirroring
            :meth:`split_tool_menu`, which likewise lists only the unrevealed.
        """
        revealed = self._get_revealed() if not include_revealed else set()
        index: dict[str, str] = {}
        for tool in self.iter_unique():
            name = tool.name
            if name not in self._deferred or name in revealed:
                continue
            index[name] = self._menu_line(tool)
        return index

    def deferred_search_index(self) -> dict[str, str]:
        """The deferred tools' MATCH corpus → ``{name: summary + keywords}``.

        The search-only sibling of :meth:`deferred_index`. The DISPLAY menu
        (``deferred_index`` / the reminder tail) stays a pure one-line summary so
        it is small and byte-stable; this corpus additionally folds in each
        tool's recall :attr:`~mote.runtime.tools.base_tool.BaseTool.keywords` (see
        :meth:`~mote.runtime.tools.base_tool.BaseTool.search_text`) so ``SearchTools``
        matches synonyms the summary omits — without those words ever reaching
        the wire or a menu. Same deferred set, same byte-stability guarantee
        (independent of the revealed set); only the per-tool text is enriched.
        """
        index: dict[str, str] = {}
        for tool in self.iter_unique():
            name = tool.name
            if name not in self._deferred:
                continue
            if isinstance(tool, ExecutableToolBinding):
                index[name] = tool.definition.search_text or self._menu_line(tool)
            else:
                index[name] = self._menu_line(tool)
        return index

    def split_tool_menu(self) -> dict[str, str]:
        """Brief-hint menu of unrevealed client-side deferred tools.

        Until reveal, these tools are absent from the native ``tools=``
        projection. This menu is therefore the model's only discovery surface:
        it carries a name plus one-line hint, without a description body or
        parameter schema. It is injected into the ephemeral reminder tail.

        Lists ONLY the not-yet-revealed corpus tools. Once a tool is revealed via
        ``SearchTools``, its complete definition enters the active schema
        projection on the next turn. Its prose is also registered as a sticky
        resource for context rebuilds, while the SearchTools result itself stays
        a short confirmation. The revealed tool therefore drops out of this
        ephemeral menu.

        Because the menu only ever shrinks (revealed tools leave), it stays a
        small, stable brief-hint index of what remains to be discovered. Each
        hint is the tool's :meth:`~mote.runtime.tools.base_tool.BaseTool.summary`
        (see :meth:`_menu_line`).
        """
        revealed = self._get_revealed()
        menu: dict[str, str] = {}
        for tool in self.iter_unique():
            name = tool.name
            if name not in self._deferred or name in revealed:
                continue
            menu[name] = self._menu_line(tool)
        return menu

    def describe_deferred(self, names: list[str]) -> dict[str, str]:
        """Full (multi-line) descriptions for the given deferred tool names.

        ``SearchTools`` reads this on reveal to persist each newly-revealed
        tool's real description into the conversation + ResourceRegistry. Only
        names in the deferred corpus resolve; unknown names are skipped.
        """
        wanted = {n for n in names if n in self._deferred}
        out: dict[str, str] = {}
        for tool in self.iter_unique():
            name = tool.name
            if name not in wanted:
                continue
            if isinstance(tool, ExecutableToolBinding):
                out[name] = tool.definition.description.strip()
        return out

    def reconstructable_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools whose results are re-derivable.

        A tool self-declares this via the ``reconstructable`` ClassVar. Every
        name a tool routes under is included so the Transcript matches whichever
        alias the model used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if tool.reconstructable:
                names.add(name)
        return frozenset(names)

    def graph_tool_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools that are graph orchestrators.

        Classification comes exclusively from the immutable definition. Every
        alias is included so the check matches whichever name a spec used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            kind = tool.definition.execution_kind
            if kind.is_workflow:
                names.add(name)
        return frozenset(names)

    def graph_excluded_tool_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools that must not be graph nodes.

        A tool self-declares this via the ``graph_excluded`` ClassVar (see
        :class:`~mote.runtime.tools.base_tool.BaseTool`). run_graph uses this to refuse
        referencing such a tool from a node — e.g. Sleep, which blocks on an
        external wake event a foreground graph never delivers. Every alias is
        included so the check matches whichever name a spec used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if tool.graph_excluded:
                names.add(name)
        return frozenset(names)


class XmlToolCatalog(BoundToolCatalog):
    """Bound execution index with the XML prompt-catalog projection only."""

    def schemas_for(self, category: str | None) -> dict[str, dict]:
        schemas: dict[str, dict] = {}
        for tool in self.iter_unique():
            if category is not None and self.category(tool) != category:
                continue
            if self._is_hidden(tool):
                continue
            if not isinstance(tool, ExecutableToolBinding) or not isinstance(tool.definition, XmlToolDefinition):
                raise TypeError("XmlToolCatalog contains a non-XML bound tool")
            schema = dict(tool.compiled_definition.rendered_schema)
            schemas[str(schema["name"])] = schema
        return schemas


class NativeToolCatalog(BoundToolCatalog):
    """Bound execution index with the provider-native projection only."""

    def schemas_for(self, category: str | None) -> dict[str, dict]:
        """Return provider-independent Native definitions for volatile views.

        Provider envelope conversion belongs exclusively to :meth:`native_specs`.
        The per-turn MCP reminder needs the canonical definitions because MCP is
        hot-reloadable and the reminder must not depend on one provider's wire
        shape.
        """

        schemas: dict[str, dict] = {}
        for tool in self.iter_unique():
            if category is not None and self.category(tool) != category:
                continue
            if self._is_hidden(tool):
                continue
            if not isinstance(tool, ExecutableToolBinding) or not isinstance(tool.definition, NativeToolDefinition):
                raise TypeError("NativeToolCatalog contains a non-Native bound tool")
            schema = dict(tool.compiled_definition.rendered_schema)
            schemas[str(schema["name"])] = schema
        return schemas

    def native_specs(self, provider: str = "anthropic") -> list[dict]:
        native: dict[str, dict] = {}
        for tool in self.iter_unique():
            if not isinstance(tool, ExecutableToolBinding) or not isinstance(tool.definition, NativeToolDefinition):
                raise TypeError("NativeToolCatalog contains a non-Native bound tool")
            if self._is_hidden(tool):
                continue
            schema = dict(tool.compiled_definition.rendered_schema)
            native[str(schema["name"])] = schema
        return to_native_tool_specs(native, provider=provider)

    def canonical_specs(self, *, include_hidden: bool = True) -> list[dict]:
        """Return provider-neutral definitions; adapters own wire projection."""

        specs: list[dict] = []
        for tool in self.iter_unique():
            if not isinstance(tool, ExecutableToolBinding) or not isinstance(tool.definition, NativeToolDefinition):
                raise TypeError("NativeToolCatalog contains a non-Native bound tool")
            if not include_hidden and self._is_hidden(tool):
                continue
            schema = dict(tool.compiled_definition.rendered_schema)
            schema["defer_loading"] = self._is_corpus(tool)
            specs.append(schema)
        return specs


__all__ = [
    "BoundToolCatalog",
    "NativeToolCatalog",
    "XmlToolCatalog",
]
