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

from typing import Any, Callable, Iterator

from mote.common.const.llm import supports_native_tool_search
from mote.executor.mcp_adapter import MCPToolAdapter
from mote.executor.tasks.bggraph.marker import is_pipeline_tool
from mote.executor.tool_spec_adapter import to_native_tool_specs

# The byte-stable stub that stands in for a deferred tool's real description on
# the native ``tools=`` wire under the SPLIT path (an incapable native model: no
# server-side ``defer_loading``). The tool's NAME + ``input_schema`` (parameters)
# stay on the wire so the model can still emit a structured, constrained call;
# only the prose DESCRIPTION is moved to the ephemeral reminder tail (the
# ``# Additional tools`` section, injected AFTER the cache breakpoint). Because
# this stub is one shared constant — identical for every corpus tool and
# independent of the revealed set — the ``tools=`` prefix is byte-stable across
# reveals, so the provider's prompt/prefix cache survives (the whole point of
# split: keep callability + cache while shedding description tokens from the
# cached prefix). Contrast with the client-side WITHHOLD path (XML), which drops
# the schema entirely and re-adds it on reveal (busting the cache).
SPLIT_TOOLSPEC_DESC = (
    "Peripheral tool — its full description is in the '# Additional tools' "
    "reminder section (call SearchTools to load it)."
)


class ToolCatalog:
    """Store + schema view for an executor's bound tools (static + dynamic).

    *Deferral* (tool-search): a subset of bound tools can be declared **deferred**
    — their full schema is withheld from both channels' schema views until the
    tool is *revealed* (the model discovers it via the ``SearchTools`` meta-tool).
    Deferral is purely about schema **visibility**, never dispatchability: a
    deferred tool is still in the ``_tools`` map, so a revealed-and-called tool
    always dispatches through :class:`~mote.executor.tool_executor.ToolExecutor`.
    The revealed set is read live via ``get_revealed`` (it lives on RoleState so
    it survives session resume), so revelation is durable without depending on
    compaction preserving history.
    """

    def __init__(
        self,
        deferred: set[str] | None = None,
        get_revealed: Callable[[], set[str]] | None = None,
    ) -> None:
        self._tools: dict[str, Any] = {}  # name -> BaseTool instance (aliases share one instance)
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

    def _is_hidden(self, tool: Any) -> bool:
        """True when *tool* is deferred and not yet revealed → schema withheld.

        A hidden tool is dropped from the schema views (both channels) but stays
        fully dispatchable — deferral scopes only *visibility*. This is the
        CLIENT-SIDE withhold/reveal predicate used by the XML channel and every
        non-Anthropic native provider.
        """
        name = getattr(tool, "name", "")
        return name in self._deferred and name not in self._get_revealed()

    def _is_corpus(self, tool: Any) -> bool:
        """True when *tool* is a deferred-corpus member (regardless of revealed).

        The wire ``defer_loading`` flag for the Anthropic server-side tool-search
        path is keyed on **corpus membership** — constant across the whole run —
        NOT on the revealed set. So even a revealed tool keeps ``defer_loading``
        on the wire, keeping the ``tools=`` prefix byte-stable (cache preserved).
        """
        return getattr(tool, "name", "") in self._deferred

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
            if self._is_hidden(tool):
                continue
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def native_specs(self, provider: str = "anthropic", model: str | None = None) -> list[dict]:
        """Return native tool-use specs for every bound tool.

        Each tool contributes a {name, description, input_schema} record (via
        ``native_schema``), wrapped into the provider envelope. Deduplicated
        like :meth:`schemas_for`.

        **Deferral is capability-gated, then provider-shaped** — there are two
        distinct native projections of the SAME deferred corpus, plus a nested
        fallback for the XML-only client-side withhold:

          - **A. server-side ``defer_loading`` (capable native model)** — keyed
            on the MODEL's capability (:func:`supports_native_tool_search`), for
            Anthropic (``tool_reference``) / OpenAI Responses (``tool_search``):
            EVERY tool's definition is emitted (the API needs them all to expand
            references) and each corpus member is stamped ``"defer_loading":
            True`` so the API excludes it from the cached prefix until discovery.

          - **B. client-side SPLIT (incapable native model)** — an old Claude /
            old GPT / a capable model reached on the Chat-Completions ``openai``
            envelope: the API has no ``defer_loading`` (it would reject / silently
            drop the field). Instead of hiding the tool (which would bust the
            prefix cache on reveal), we keep the corpus tool PRESENT with its real
            NAME + ``input_schema`` but swap its prose ``description`` for the one
            constant :data:`SPLIT_TOOLSPEC_DESC` stub — the full description rides
            the ephemeral reminder tail instead (see :meth:`split_tool_menu`). No
            ``defer_loading`` stamp. The corpus tool is still fully callable
            (name + params on the wire → structured/constrained decoding), and
            because the stub is constant and revealed-set-independent the
            ``tools=`` prefix is byte-stable across reveals → prompt cache
            preserved. This is the whole-point of split: callability + cache while
            shedding only the description tokens from the cached prefix.

        Both A and B key the corpus on :meth:`_is_corpus` (constant across the
        run), so the wire never churns when a tool is revealed. Only the XML
        channel keeps the true client-side WITHHOLD (``schemas_for`` +
        :meth:`_is_hidden`) — there is no ``tools=`` prefix to protect there.
        """
        prov = provider.lower()
        server_defer = (
            bool(self._deferred)
            and supports_native_tool_search(model)
            and prov
            in (
                "anthropic",
                "openai_responses",
            )
        )
        # SPLIT applies whenever the role defers tools but the native transport
        # has no server-side path (incapable model, or the Chat-Completions
        # envelope). It is the native default for the non-capable case — the wire
        # never withholds a corpus tool's callable shape.
        split = bool(self._deferred) and not server_defer
        native: dict[str, dict] = {}
        defer_names: set[str] = set()
        for tool in self.iter_unique():
            schema = tool.native_schema()
            if server_defer and self._is_corpus(tool):
                # Path A: keep the full schema; mark it deferred for the API.
                defer_names.add(getattr(tool, "name", ""))
            elif split and self._is_corpus(tool):
                # Path B: keep name + params, swap description for the constant
                # stub (the full text is injected into the reminder tail). Copy
                # so the tool's cached class schema is never mutated.
                schema = {**schema, "description": SPLIT_TOOLSPEC_DESC}
            native[schema["name"]] = schema
        specs = to_native_tool_specs(native, provider=provider, model=model)
        if defer_names:
            for spec in specs:
                if spec.get("name") in defer_names:
                    spec["defer_loading"] = True
        return specs

    @staticmethod
    def _menu_line(tool: Any) -> str:
        """The one-line blurb for *tool* in a search menu.

        The tool's :meth:`~mote.executor.base_tool.BaseTool.summary` — the first
        line of its ``call()`` docstring, a tight capability-first sentence
        authored for exactly this listing. This is the single seam both menu
        builders share, so the derivation is made in one place. Falls back to
        the tool's name if it exposes no ``summary`` (e.g. a bare stand-in).
        """
        name = str(getattr(tool, "name", ""))
        summary = getattr(tool, "summary", None)
        if callable(summary):
            return str(summary() or name)
        return name

    def deferred_index(self, *, include_revealed: bool = True) -> dict[str, str]:
        """The compact menu of deferred tools → ``{name: one-line desc}``.

        Only names + one-line summaries (no parameters) so it stays small. Each
        line is the tool's :meth:`~mote.executor.base_tool.BaseTool.summary` (see
        :meth:`_menu_line`).

        Two callers with different needs select via *include_revealed*:

          - ``True`` (default) — the whole deferred corpus regardless of the
            revealed set. This is the identity/validation view (e.g.
            :meth:`~mote.roles.role.Role.reveal_tools` checks a name against it):
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
            name = getattr(tool, "name", "")
            if name not in self._deferred or name in revealed:
                continue
            index[name] = self._menu_line(tool)
        return index

    def deferred_search_index(self) -> dict[str, str]:
        """The deferred tools' MATCH corpus → ``{name: summary + keywords}``.

        The search-only sibling of :meth:`deferred_index`. The DISPLAY menu
        (``deferred_index`` / the reminder tail) stays a pure one-line summary so
        it is small and byte-stable; this corpus additionally folds in each
        tool's recall :attr:`~mote.executor.base_tool.BaseTool.keywords` (see
        :meth:`~mote.executor.base_tool.BaseTool.search_text`) so ``SearchTools``
        matches synonyms the summary omits — without those words ever reaching
        the wire or a menu. Same deferred set, same byte-stability guarantee
        (independent of the revealed set); only the per-tool text is enriched.
        """
        index: dict[str, str] = {}
        for tool in self.iter_unique():
            name = getattr(tool, "name", "")
            if name not in self._deferred:
                continue
            search_text = getattr(tool, "search_text", None)
            index[name] = str(search_text()) if callable(search_text) else self._menu_line(tool)
        return index

    def split_tool_menu(self) -> dict[str, str]:
        """Brief-hint menu of the UNREVEALED split-path deferred tools.

        The complement of the split ``tools=`` projection (see
        :meth:`native_specs` path B): the corpus tool's NAME + ``input_schema``
        ride the wire with a stub description, and this menu carries a one-line
        HINT — injected into the ephemeral reminder tail (after the cache
        breakpoint) so it never touches the byte-stable ``tools=`` prefix.

        Lists ONLY the not-yet-revealed corpus tools. Once a tool is revealed via
        ``SearchTools`` its FULL description is *persisted* into the conversation
        (the SearchTools result body, also registered as a sticky resource so it
        survives compaction) — so it enters the cached prefix and is paid once,
        instead of being re-sent uncached on this reminder tail every turn. A
        revealed tool therefore drops OUT of this ephemeral menu.

        Because the menu only ever shrinks (revealed tools leave), it stays a
        small, stable brief-hint index of what remains to be discovered. Each
        hint is the tool's :meth:`~mote.executor.base_tool.BaseTool.summary`
        (see :meth:`_menu_line`).
        """
        revealed = self._get_revealed()
        menu: dict[str, str] = {}
        for tool in self.iter_unique():
            name = getattr(tool, "name", "")
            if name not in self._deferred or name in revealed:
                continue
            menu[name] = self._menu_line(tool)
        return menu

    def describe_deferred(self, names: list[str]) -> dict[str, str]:
        """Full (multi-line) descriptions for the given deferred tool names.

        The prose the SPLIT path strips off the ``tools=`` wire (:data:`
        SPLIT_TOOLSPEC_DESC`). ``SearchTools`` reads this on reveal to persist
        each newly-revealed tool's real description into the conversation +
        ResourceRegistry (so it enters the cached prefix and survives
        compaction), rather than re-sending it on the reminder tail. Only names
        in the deferred corpus resolve; unknown names are skipped.
        """
        wanted = {n for n in names if n in self._deferred}
        out: dict[str, str] = {}
        for tool in self.iter_unique():
            name = getattr(tool, "name", "")
            if name not in wanted:
                continue
            schema = tool.tool_schema()
            out[name] = (schema.get("description", "") or "").strip()
        return out

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

    def graph_excluded_tool_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools that must not be graph nodes.

        A tool self-declares this via the ``graph_excluded`` ClassVar (see
        :class:`~mote.executor.base_tool.BaseTool`). run_graph uses this to refuse
        referencing such a tool from a node — e.g. Sleep, which blocks on an
        external wake event a foreground graph never delivers. Every alias is
        included so the check matches whichever name a spec used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if getattr(tool, "graph_excluded", False):
                names.add(name)
        return frozenset(names)
