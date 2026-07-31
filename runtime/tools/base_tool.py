#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BaseTool — base class for all tools.

Tools inherit BaseTool and implement call(**kwargs).
All kwargs are LLM-specified parameters. The only framework context injected
is session_id (via bind(session_id) before call()).

Declaration: Product-owned builtins are listed in an immutable Application catalog.
Instance management: ToolExecutor creates and caches instances per-Role.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol

from mote.contracts.authorization import PermissionDecision
from mote.contracts.config.tool import DEFAULT_MAX_RESULT_SIZE_CHARS
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.execution import ToolExecutionKind


class ToolCapabilityProvider(Protocol):
    def tool_capabilities(self) -> dict[str, Any]: ...


class BaseTool(ABC):
    """Base class for tools. Single call() entry point.

    Subclass contract:
    - Set `name` (primary), optionally `aliases` (alternative names).
    - Product builtins and external integrations enter an Application catalog
      through their Product-owned composition declarations.
    - Implement call(**kwargs) with type hints and docstring.
    - All call() parameters are LLM-specified. Framework context is self.session_id.
    - If the tool needs Role behavior, list the method names in `requires`;
      bind() injects exactly those (resolved via Role.tool_capabilities()) and
      nothing else.

    For class-based Product tools, prose is docstring-native: protocol-specific
    definition builders read the ``call()`` docstring as their source text.
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
      author once in the docstring. Runtime-discovered capabilities such as MCP
      receive explicit XML or Native definitions outside the capability.

    - Product definition builders derive schemas from the call() signature.
      Scalar params (str/int/float/bool) and pydantic models work out of the box;
      annotate a structured Native parameter with a pydantic ``BaseModel`` (or
      ``list[Model]``) to get a correct nested schema automatically.

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
    # mote.runtime.resources.spill). The effective threshold is
    # this value clamped by the system-wide default; override per tool to allow
    # larger (e.g. Read) or smaller (e.g. Sleep) results.
    max_result_size_chars: ClassVar[int] = DEFAULT_MAX_RESULT_SIZE_CHARS

    # Whether this tool fronts a live, per-Role runtime (a persistent shell,
    # Python kernel, browser, ...). Managed tools register drivers with the
    # Role's RuntimeHost so identity, access, fencing and teardown are shared.
    # Stateless tools (the default) hold no state between calls.
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

    execution_kind: ClassVar[ToolExecutionKind] = ToolExecutionKind.ATOMIC

    # Whether this tool must NOT appear as a node inside a declarative graph.
    # Distinct from execution_kind: this marks tools whose behaviour is meaningless — or actively
    # harmful — inside a non-interactive batch orchestration. Sleep is the case:
    # it blocks the coroutine until an *external* wake event (a new message or a
    # background-task completion), and a foreground graph run delivers neither,
    # so a Sleep node would hang the whole graph indefinitely. run_graph refuses
    # to reference any such tool from a node. Consumed by the ToolExecutor to
    # expose the excluded-tool name set to the run_graph orchestrator.
    graph_excluded: ClassVar[bool] = False

    # --- Permission metadata (consumed by the PermissionEngine) ---
    # Coarse risk label a tool self-declares (advisory in phase 1). See
    # mote.contracts.authorization.RiskLevel.
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

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            getattr(type(self), "stateful", False)
            and name != "_session_id"
            and name not in getattr(type(self), "requires", ())
        ):
            raise AttributeError(
                f"stateful tool '{type(self).__name__}' cannot own mutable "
                f"instance field '{name}'; put business state in its RuntimeDriver"
            )
        object.__setattr__(self, name, value)

    def bind(self, session_id: str, role: ToolCapabilityProvider | None = None) -> "BaseTool":
        """Bind context to this tool instance. Returns self for chaining.

        Called by the framework (ToolExecutor) at tool creation time.
        Injects ONLY the narrow Role capabilities named in `requires`, resolved
        against the Role's explicit capability allowlist (Role.tool_capabilities)
        — never via getattr on the Role — so a tool can never reach RoleState,
        memory, or any Role attribute that is not deliberately published.
        """
        if self.stateful and "get_runtime_host" not in self.requires:
            raise TypeError(f"stateful tool '{type(self).__name__}' must declare the " "get_runtime_host capability")
        if self.stateful and "handoff_runtime" not in self.requires:
            raise TypeError(f"stateful tool '{type(self).__name__}' must declare the " "handoff_runtime capability")
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

    def can_resume_started_call(self, call_id: str) -> bool:
        """Whether this tool can safely reconcile a ledgered started call."""
        return False

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

    def resolve_effect_for(self, args: dict[str, Any]) -> ToolEffect:
        """Resolve the effect class for one invocation.

        Most tools have one effect class and inherit this implementation. A
        multi-action tool may override it when one action crosses a different
        side-effect boundary, such as a LOCAL canvas whose handoff action opens
        an EXTERNAL human interaction.
        """
        return self.resolve_effect()

    # ------------------------------------------------------------------
    # Permission hooks (consumed by the PermissionEngine before call())
    # ------------------------------------------------------------------

    def permission_target(self, args: dict) -> str:
        """Return the string matched against rule patterns for this call.

        E.g. ``Bash`` returns its ``command``; file tools return an absolute,
        symlink-resolved path. The default is empty, meaning only whole-tool
        rules (``Tool`` without a pattern) can match. Override to enable
        ``Tool(pattern)`` rules. Every filesystem-mutating tool must expose each
        path it can write here or through :meth:`permission_targets`; a non-full
        sandbox rejects a write with no concrete target.
        """
        return ""

    def mutates_filesystem_for(self, args: dict) -> bool:
        """Return whether this invocation writes to the filesystem.

        Most tools are statically read-only or file-mutating and use the class
        declaration above. Tools with an optional local-output argument may
        override this without turning their read-only calls into writes.
        """
        return self.mutates_filesystem

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
