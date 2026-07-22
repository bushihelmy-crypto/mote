#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BaseTool — base class for all tools.

Tools inherit BaseTool and implement call(**kwargs).
All kwargs are LLM-specified parameters. The only framework context injected
is session_id (via bind(session_id) before call()).

Registration: Use the @register_tool decorator from tool_registry.
Instance management: ToolExecutor creates and caches instances per-Role.
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from mote.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolEffect
from mote.common.schema.permission_types import PermissionDecision
from mote.common.utils.docstring import description_body, first_line
from mote.executor.tool_convert import function_docstring_to_schema
from mote.executor.tool_spec_adapter import build_json_schema


class BaseTool(ABC):
    """Base class for tools. Single call() entry point.

    Subclass contract:
    - Set `name` (primary), optionally `aliases` (alternative names).
    - Decorate with @register_tool to register.
    - Implement call(**kwargs) with type hints and docstring.
    - All call() parameters are LLM-specified. Framework context is self.session_id.
    - If the tool needs Role behavior, list the method names in `requires`;
      bind() injects exactly those (resolved via Role.tool_capabilities()) and
      nothing else.

    Prose is docstring-native — the ``call()`` docstring is the SINGLE source
    for everything the model reads about a tool, projected two ways:
    - The docstring body (everything before the first ``Args:``/``Returns:``
      section) is the full model-facing DESCRIPTION sent on the wire. Write it
      as a normal operating manual: a tight one-line summary first, then a blank
      line, then as much detail as the tool warrants.
    - That first summary line is also the tool's one-line MENU entry (see
      :meth:`summary`) — the blurb shown in the tool-search catalogue for a
      deferred tool, before its full description is revealed. So keep line one a
      self-contained, capability-first sentence.
    - The ``Args:`` block documents parameters; it is parsed into the schema
      separately (never duplicated into the wire description).
    There is no ``description`` class attribute and no separate menu field:
    author once in the docstring. Override :meth:`custom_schema` /
    :meth:`get_native_schema` only for dynamic params or a runtime description
    (e.g. MCP, or WebSearch injecting the current month/year).

    - Schema is auto-generated from call() signature — no manual schema needed.
      Scalar params (str/int/float/bool) and pydantic models work out of the box;
      annotate a structured param with a pydantic ``BaseModel`` (or ``list[Model]``)
      to get a correct nested schema automatically. Override custom_schema() /
      get_native_schema() only for dynamic params (e.g. MCP).

    Channel limitation (IMPORTANT):
    - The XML command protocol parses EVERY argument as a string — it does
      not carry parameter types, so list/dict/model params arrive as raw strings
      there. Tools with structured (non-scalar) params therefore work correctly
      ONLY on the native tool-use channel. Keep tool params scalar if the tool
      must run under the XML protocol; otherwise restrict the tool to native.

    Lifecycle:
    - Instances are managed by ToolExecutor (per-Role isolation).
    - bind(session_id) called by ToolExecutor at creation time.
    - cleanup_session() called when a Role exits.
    """

    # --- Identity ---
    name: ClassVar[str] = ""  # Primary tool name
    aliases: ClassVar[list[str]] = []  # Alternative names (LLM can use any)
    # Recall-only search vocabulary for the tool-search MATCH corpus — words a
    # model might reasonably use to *look for* this tool that its one-line summary
    # does not already contain (synonyms, common phrasings, sibling terms). These
    # are the THIRD, dedicated layer of a strict three-way split:
    #   - DISPLAY  = the one-line summary (menu / reminder tail; token-sensitive)
    #   - DISPATCH = name + aliases (callable lookup keys)
    #   - SEARCH   = name + summary + THESE keywords (recall corpus only)
    # So a keyword raises SearchTools' hit rate WITHOUT bloating the menu the
    # model reads every turn and WITHOUT becoming a callable name (unlike an
    # alias). Never rendered on the wire, never shown in any menu — consumed only
    # by the search matcher (see :meth:`search_text` and
    # ToolCatalog.deferred_search_index). Add a term here only when it is a real
    # recall gap; keep the list tight (curated, not a keyword-stuffing dump) so
    # matching stays precise.
    keywords: ClassVar[list[str]] = []
    # Names of Role capabilities (methods) this tool needs. bind() injects ONLY
    # these, resolved against Role.tool_capabilities() (an explicit allowlist).
    # A name not published there is rejected; the tool never receives RoleState,
    # memory, or the Role object itself.
    requires: ClassVar[tuple[str, ...]] = ()

    # Cap on this tool's result size, in characters. When a single call's text
    # output exceeds this, the framework persists the full result to disk and
    # replaces the inline content with a <persisted-output> preview (see
    # mote.executor.tool_result_limit). The effective threshold is
    # this value clamped by the system-wide default; override per tool to allow
    # larger (e.g. Read) or smaller (e.g. Sleep) results.
    max_result_size_chars: ClassVar[int] = DEFAULT_MAX_RESULT_SIZE_CHARS

    # Whether this tool keeps live, per-Role session state (a persistent shell,
    # a Python kernel, ...). A stateful tool stores its live session on the
    # owning Role's RoleState — via the get_tool_session / set_tool_session
    # capabilities — instead of a process-global singleton, so the state is
    # owned by the Role, isolated per session, and torn down with it (no
    # cross-session leakage). Stateless tools (the default) hold no state
    # between calls.
    stateful: ClassVar[bool] = False

    # Whether this tool's result is *reconstructable* — re-derivable by re-running
    # the tool (a read-only or idempotent observation like Read/Search, or a
    # write whose effect is durable on disk like Write/Edit). The compaction
    # pipeline may fold/clear a reconstructable result's body in place, since the
    # information is recoverable from the live filesystem on demand. Tools whose
    # result is a one-shot side effect or a user interaction that cannot be
    # replayed (AskUserQuestion, Agent spawns, Sleep, ...) must leave this False so
    # their bodies are preserved verbatim. Consumed by the ToolExecutor to build
    # the per-Role compactable set the ContextManager threads into the Transcript.
    reconstructable: ClassVar[bool] = False

    # Whether this tool is itself a *graph orchestrator* — it drives a bggraph
    # internally (RunGraph, CodeReview). run_graph refuses to
    # reference any such tool from a node, so a declarative graph can never nest
    # another graph (no run_graph→run_graph recursion, and no run_graph→CodeReview
    # graph-in-graph). Tools that merely *call* other tools are fine; this marks
    # only tools whose body is a compiled bggraph. Consumed by the ToolExecutor
    # to expose the graph-tool name set to the run_graph orchestrator.
    is_graph_tool: ClassVar[bool] = False

    # Whether this tool must NOT appear as a node inside a declarative graph
    # (run_graph). Distinct from ``is_graph_tool`` (which blocks graph-in-graph
    # nesting): this marks tools whose behaviour is meaningless — or actively
    # harmful — inside a non-interactive batch orchestration. Sleep is the case:
    # it blocks the coroutine until an *external* wake event (a new message or a
    # background-task completion), and a foreground graph run delivers neither,
    # so a Sleep node would hang the whole graph indefinitely. run_graph refuses
    # to reference any such tool from a node. Consumed by the ToolExecutor to
    # expose the excluded-tool name set to the run_graph orchestrator.
    graph_excluded: ClassVar[bool] = False

    # --- Permission metadata (consumed by the PermissionEngine) ---
    # Coarse risk label a tool self-declares (advisory in phase 1). See
    # mote.common.schema.permission_types.RiskLevel.
    risk_level: ClassVar[str] = "low"
    # Whether this tool mutates the filesystem. Drives the ``acceptEdits``
    # permission mode (auto-approve edits). Set True on file-writing tools.
    mutates_filesystem: ClassVar[bool] = False

    # This tool's side-effect class, used by the effect ledger to decide whether
    # an interrupted call is safe to replay after a crash. ``None`` (the default)
    # means "derive from existing metadata" (see :meth:`resolve_effect`): a
    # filesystem-mutating tool derives LOCAL, everything else derives the
    # conservative EXTERNAL. A tool sets this explicitly only when that
    # derivation is wrong for it — e.g. a read-only observation (Read/Search)
    # declares PURE to opt out of ledgering, or a provably-idempotent external
    # tool declares its narrower class. Consumed by the ToolExecutor at the
    # run_command chokepoint — only EXTERNAL calls are ledgered and guarded
    # against blind re-execution.
    effect: ClassVar[ToolEffect | None] = None

    # --- Execution ---

    def __init__(self) -> None:
        self._session_id: str = ""

    def bind(self, session_id: str, role=None) -> "BaseTool":
        """Bind context to this tool instance. Returns self for chaining.

        Called by the framework (ToolExecutor) at tool creation time.
        Injects ONLY the narrow Role capabilities named in `requires`, resolved
        against the Role's explicit capability allowlist (Role.tool_capabilities)
        — never via getattr on the Role — so a tool can never reach RoleState,
        memory, or any Role attribute that is not deliberately published.
        """
        self._session_id = session_id
        if role is not None:
            capabilities = role.tool_capabilities()
            for name in self.requires:
                if name not in capabilities:
                    raise AttributeError(
                        f"{type(self).__name__}.requires '{name}', but it is not a "
                        f"capability published by Role.tool_capabilities()"
                    )
                setattr(self, name, capabilities[name])
        return self

    @property
    def session_id(self) -> str:
        """The session_id of the owning Role."""
        return self._session_id

    @abstractmethod
    async def call(self, **kwargs) -> Any:
        """Tool entry point. All kwargs are LLM-specified parameters.

        Schema is auto-generated from this method's signature and docstring.
        """

    def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session resources when a Role exits. Default no-op."""

    @classmethod
    def resolve_effect(cls) -> ToolEffect:
        """This tool's resolved side-effect class (never ``None``).

        Returns the explicit :attr:`effect` when a tool declares one; otherwise
        derives it from existing metadata, conservatively:

        - ``mutates_filesystem`` → ``LOCAL`` (fs mutation, already protected by
          the before-image snapshot layer, so replay-safe);
        - else → ``EXTERNAL`` (unknown effect surface — guarded by default so an
          untagged tool that reaches the network / a subprocess / a human is
          never silently replayed after a crash).

        Note ``reconstructable`` is deliberately *not* a signal here: it means
        "result re-derivable" (a compaction concern) and does NOT imply
        side-effect-free — ``Bash`` is ``reconstructable`` yet plainly EXTERNAL.
        The genuinely read-only tools (Read/Search) opt out of ledgering by
        declaring ``effect = ToolEffect.PURE`` explicitly.
        """
        if cls.effect is not None:
            return cls.effect
        if cls.mutates_filesystem:
            return ToolEffect.LOCAL
        return ToolEffect.EXTERNAL

    # ------------------------------------------------------------------
    # Permission hooks (consumed by the PermissionEngine before call())
    # ------------------------------------------------------------------

    def permission_target(self, args: dict) -> str:
        """Return the string matched against rule patterns for this call.

        E.g. ``Bash`` returns its ``command``, file tools return the path. The
        default is empty, meaning only whole-tool rules (``Tool`` without a
        pattern) can match. Override to enable ``Tool(pattern)`` rules.
        """
        return ""

    def permission_targets(self, args: dict) -> list[str]:
        """Return *all* permission-target strings this call touches.

        The default wraps :meth:`permission_target` (single target) so existing
        tools are unaffected. A tool that acts on multiple paths in one call
        overrides this to list every path; the executor
        then evaluates them together via ``PermissionEngine.check_multi``.
        """
        target = self.permission_target(args)
        return [target] if target else []

    def permission_segments(self, args: dict) -> "list[str] | None":
        """Split a shell command into independently-evaluated segments.

        Shell-command tools (``Bash``/``Terminal``) override this to return the
        command split on ``&&  ||  ;  |`` so the permission engine resolves rules
        per segment (a deny rule catches the dangerous half of ``ls && rm -rf``)
        and can remember an approval as a stable *prefix* rule. ``None`` (the
        default) means "not a command" — the engine matches the whole target.
        """
        return None

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        """Tool-specific permission self-check, run before mode/rule fallback.

        Return a :class:`PermissionDecision` to force ``allow``/``deny``/``ask``
        (deny/ask here are bypass-immune safety checks), or ``None`` to defer to
        rules and the permission mode. Default defers.
        """
        return None

    def tool_schema(self) -> dict:
        """Return this tool's LLM-facing schema (instance-level).

        Default delegates to the class-level get_schema(). Dynamic tools whose
        name/parameters are only known at runtime (e.g. MCP) override this to
        return their own schema.
        """
        return type(self).get_schema()

    @classmethod
    def summary(cls) -> str:
        """This tool's one-line summary — the tool-search MENU entry.

        The first line of the ``call()`` docstring, which by convention is a
        tight, self-contained "what is this for" sentence. Used by the catalogue
        that lists a role's deferred tools before their full descriptions are
        revealed. A dynamic-description tool (custom_schema override) derives it
        from that schema's description instead, so the menu always tracks the
        real wire prose.
        """
        custom = cls.custom_schema()
        if custom is not None:
            return first_line(custom.get("description", ""))
        return first_line(inspect.getdoc(cls.call))

    @classmethod
    def search_text(cls) -> str:
        """The tool-search MATCH corpus for this tool — summary + recall keywords.

        The one-line :meth:`summary` (what the model *sees* in the menu) joined
        with the tool's dedicated :attr:`keywords` (recall vocabulary the model
        never sees). This is the text SearchTools matches a query against — the
        SEARCH layer of the display/dispatch/search split — so a synonym in
        ``keywords`` lifts the hit rate without touching the menu the model reads
        or the callable names. Menus keep using :meth:`summary` alone.
        """
        summary = cls.summary()
        if not cls.keywords:
            return summary
        return f"{summary} {' '.join(cls.keywords)}"

    @classmethod
    def get_schema(cls) -> dict:
        """Compute this tool's schema. A tool owns its own schema — the registry
        only registers and looks up, it does not describe.

        Uses custom_schema() if overridden, otherwise auto-generates from the
        call() docstring: the docstring body (everything before ``Args:``) is the
        model-facing description, and the ``Args:`` block yields the parameters.
        """
        custom = cls.custom_schema()
        if custom is not None:
            return custom

        docstring = inspect.getdoc(cls.call) or ""
        params = function_docstring_to_schema(cls.call, docstring)
        return {
            "name": cls.name,
            "description": description_body(docstring),
            "parameters": params,
        }

    @classmethod
    def custom_schema(cls) -> dict | None:
        """Override to provide a custom schema instead of auto-generation.

        Return a dict with "name", "description", "parameters" keys,
        or None to use the default auto-generated schema.
        """
        return None

    def native_schema(self) -> dict:
        """Return this tool's native tool-use schema (instance-level).

        Mirrors tool_schema() but produces a structured JSON Schema for the
        parameters (under "input_schema") instead of the XML protocol's
        free-text "parameters". Default delegates to the class-level
        get_native_schema(); dynamic tools (e.g. MCP) override this to return a
        schema whose params are already JSON Schema.
        """
        return type(self).get_native_schema()

    @classmethod
    def get_native_schema(cls) -> dict:
        """Compute this tool's native tool-use schema.

        Returns {"name", "description", "input_schema"} where input_schema is a
        JSON Schema object built from the call() signature + docstring. Used by
        the native tool-use channel; the XML path keeps using get_schema().
        """
        base = cls.get_schema()
        return {
            "name": base["name"],
            "description": base["description"],
            "input_schema": build_json_schema(cls.call),
        }
