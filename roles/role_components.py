#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleComponents — declarative assembly + ownership of a Role's subsystems.

The Role is pure orchestration; the wiring of its (mostly opt-in, lazily built)
collaborators — the LLM router, tool executor, context manager, event bus,
session log, hook/LSP/file-watch services, the per-turn context bus, etc. —
lives here. The Role keeps the public property surface (``role.executor``,
``role.event_bus`` …) as thin delegators onto this object, so external callers
and tests are unchanged.

Each collaborator is declared once as a :class:`ComponentSpec` in
:meth:`RoleComponents._component_specs` and resolved by a single lazy,
cycle-checked :class:`ComponentGraph`. A builder reads its siblings only through
``ctx.dep`` (eager, cycle-tracked) / ``ctx.defer`` (a lazy thunk), and role facts
through ``ctx.role`` / ``ctx.state`` — so *construction* is a pure DAG (a build
cycle raises :class:`ComponentCycleError` at the traversal, never a stack
overflow) and no builder mutates a sibling. The two genuinely cyclic runtime
cross-references (the event spine ⇄ its subscribers, and the router ← context
manager reducer edge) are layered on *afterward* by explicit lifecycle steps
(:meth:`_wire_spine` / :meth:`_wire_collaborators`), driven from
``Role._ensure_ready`` before the first turn.

Mutable extras that are not themselves components (queued Python hook callbacks,
the pending task-completion wake, the live resource-cap guard) live on
:class:`ComponentsState`, threaded to builders via ``ctx.state``. ``peek_*``
accessors expose a built slot without triggering a build (teardown paths).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from metagpt.common.config.loader import load_config
from metagpt.common.config.sources import discover_source_files
from metagpt.common.const import METAGPT_REPORTER_DEFAULT_URL
from metagpt.common.events import EventBus, LogSubscriber
from metagpt.common.hook import HookManager
from metagpt.common.hook.subscriber import HookSubscriber
from metagpt.common.interface import ObservationSubscriber
from metagpt.common.logs import logger
from metagpt.common.observability.langfuse_backend import LangfuseBackend
from metagpt.common.observability.langfuse_integration import (
    is_enabled,
    step_tracing_enabled,
)
from metagpt.common.observability.tracing import TracingSubscriber
from metagpt.common.resource import ResourceRegistry
from metagpt.common.schema import SandboxConfig
from metagpt.common.utils.report import ReporterSubscriber
from metagpt.context import ContextManager
from metagpt.context.compaction import FileRehydrator
from metagpt.context.skills.skill_manager import SkillManager
from metagpt.context.skills.skill_pool import _BUILTIN_DIR
from metagpt.context.turn_context import (
    ChangedFilesContextSource,
    CodeMapContextSource,
    CompactionNoticeContextSource,
    GitContextSource,
    SkillActivationContextSource,
    SkillListingContextSource,
    TokenPressureContextSource,
    ToolCatalogContextSource,
    TurnContextBus,
)
from metagpt.environment.watching import FileWatchService
from metagpt.executor.permission.sandbox import (
    ResourceGuard,
    SandboxGuard,
    build_runtime,
)
from metagpt.executor.mcp.config_source import mcp_config_path
from metagpt.executor.tasks import BackgroundTaskPool, TaskOutputStore
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.loop import BaseLoop, ReActLoop
from metagpt.parser import (
    CommandChannel,
    infer_native_tool_provider,
    make_command_channel,
)
from metagpt.roles.capabilities import RoleCapabilities
from metagpt.roles.component_graph import BuildContext, ComponentGraph, ComponentSpec
from metagpt.roles.context_provider import ContextProvider
from metagpt.roles.lsp import DiagnosticsBuffer, LspService
from metagpt.roles.role_state import RoleStateController
from metagpt.roles.session_manager import RoleSessionManager
from metagpt.router.router import COMPRESSION_TASK, LLMRouter
from metagpt.session import (
    BrowserStateRecorder,
    FileSnapshotRecorder,
    KernelStateRecorder,
    SessionLog,
    SessionMetaEvent,
    TerminalStateRecorder,
)
from metagpt.session.snapshot import detect_blob_backend
from metagpt.session.subscribers import RecorderSubscriber
from metagpt.common.base import BaseThinkEngine
from metagpt.think.prompt_builder import ThinkSubsystems
from metagpt.think.think_engine import ThinkEngine

if TYPE_CHECKING:
    from metagpt.roles.role import Role

class ComponentsState:
    """Mutable per-Role extras that are not themselves graph components.

    Threaded into every builder via ``ctx.state`` so a spec can read/adjust them
    without reaching for a sibling: the queued Python hook callbacks (seeded into
    the HookManager on build), the pending task-completion wake (stashed before
    the pool exists), and the live resource-cap guard (set by the sandbox builder,
    read by ``peek_resource_guard``).
    """

    def __init__(self) -> None:
        self.pending_task_completion_wake: "Optional[Callable]" = None
        self.hook_callbacks: list[tuple[str, Any, Optional[str]]] = []
        self.resource_guard: Optional[ResourceGuard] = None


class RoleComponents:
    """Owns and lazily builds the Role's collaborators (see module docstring)."""

    def __init__(self, role: "Role"):
        self._role = role
        self._state = ComponentsState()
        self._graph = ComponentGraph(role, self._state, self._component_specs())
        # The spine is wired exactly once, as an explicit lifecycle step
        # (``_wire_spine`` from ``Role._ensure_ready``), never as a side-effect of
        # constructing the leaf ``event_bus``. Set True *before* the wiring body
        # runs so a redundant/re-entrant call short-circuits (idempotent).
        self._spine_wired = False
        # Runtime cross-references between already-built collaborators (e.g. the
        # router ← ContextManager reducer edge), established once as an explicit
        # lifecycle step (``_wire_collaborators`` from ``Role._ensure_ready``) so
        # no getter mutates a *sibling* component as a hidden read side-effect.
        self._collaborators_wired = False

    # =========================================================================
    # Declarative component registry
    # =========================================================================

    def _component_specs(self) -> list[ComponentSpec]:
        """The single declarative list of every buildable collaborator.

        One entry per component: its name (= the Role's public attribute), a pure
        ``build`` of a ``BuildContext``, and an optional ``available`` gate for the
        opt-in layers. Read top-to-bottom, this is the Role's whole subsystem
        graph as data; the resolver (:class:`ComponentGraph`) turns it into lazy,
        cycle-checked access.
        """
        return [
            # --- leaves (read no sibling) -----------------------------------
            # Behaviour holders over the Role: the state controller reads only the
            # (pure-DTO) RoleState, the capabilities holder only the Role itself.
            # Registered as leaves so the Role's ``_state_ctl`` / ``_capabilities``
            # delegators resolve them through the one graph like every other
            # collaborator (Role.__init__ builds nothing but this holder).
            ComponentSpec("state_ctl", lambda ctx: RoleStateController(ctx.role.state)),
            ComponentSpec("capabilities", lambda ctx: RoleCapabilities(ctx.role)),
            ComponentSpec("session_manager", lambda ctx: RoleSessionManager(ctx.role)),
            ComponentSpec("router", _build_router),
            ComponentSpec("skill_manager", _build_skill_manager),
            ComponentSpec("bg_pool", _build_bg_pool),
            ComponentSpec("resource_registry", lambda ctx: ResourceRegistry()),
            ComponentSpec("session_log", _build_session_log),
            ComponentSpec("event_bus", lambda ctx: EventBus()),
            ComponentSpec("command_channel", _build_command_channel),
            ComponentSpec("context_provider", lambda ctx: ContextProvider(ctx.role)),
            # --- opt-in leaves ----------------------------------------------
            ComponentSpec("hook_manager", _build_hook_manager, available=_hook_available),
            ComponentSpec("lsp_service", _build_lsp_service, available=_lsp_available),
            ComponentSpec("diagnostics_buffer", lambda ctx: DiagnosticsBuffer(), available=_lsp_available),
            ComponentSpec("sandbox_runtime", _build_sandbox_runtime, available=_sandbox_available),
            # --- session-log-backed recorders (eager dep: session_log) ------
            ComponentSpec("file_snapshot_recorder", _build_file_snapshot_recorder),
            ComponentSpec("terminal_state_recorder", _build_terminal_state_recorder),
            ComponentSpec("kernel_state_recorder", _build_kernel_state_recorder),
            ComponentSpec("browser_state_recorder", _build_browser_state_recorder),
            # --- one-sibling-edge nodes -------------------------------------
            ComponentSpec("executor", _build_executor),  # eager event_bus; defers bg_pool
            ComponentSpec("context_manager", _build_context_manager),  # eager registry/router/event_bus
            ComponentSpec("turn_context_sources", _build_turn_context_sources),  # eager diagnostics_buffer
            # --- L2 (read an L1 sibling) ------------------------------------
            ComponentSpec("turn_context_bus", lambda ctx: TurnContextBus(ctx.dep("turn_context_sources"))),
            # --- per-turn factories (cached factory, fresh instance per turn) -
            # These resolve to a *callable* (not an instance): the factory reads
            # the ``*_kind`` schema knob at call-time and builds a fresh instance
            # each turn, so a strategy swapped mid-session is honoured next turn
            # while the graph still caches only the stateless factory (a pure
            # DAG). ``think_engine`` is stateless machinery (its per-turn result
            # now lives on RoleState), so a fresh one per run() is free.
            ComponentSpec("think_engine_factory", _build_think_engine_factory),
            ComponentSpec("think_subsystems_factory", _build_think_subsystems_factory),
            ComponentSpec("loop_factory", _build_loop_factory),
            # --- side-effectful builder (may return None) -------------------
            ComponentSpec("file_watch_service", _build_file_watch_service),
        ]

    # =========================================================================
    # Public property surface — thin delegators onto the graph
    # =========================================================================

    @property
    def state_ctl(self) -> RoleStateController:
        return self._graph.get("state_ctl")

    @property
    def capabilities(self) -> RoleCapabilities:
        return self._graph.get("capabilities")

    @property
    def session_manager(self) -> RoleSessionManager:
        return self._graph.get("session_manager")

    @property
    def router(self) -> LLMRouter:
        return self._graph.get("router")

    @property
    def skill_manager(self) -> SkillManager:
        return self._graph.get("skill_manager")

    @property
    def bg_pool(self) -> BackgroundTaskPool:
        return self._graph.get("bg_pool")

    @property
    def executor(self) -> ToolExecutor:
        return self._graph.get("executor")

    @property
    def context_manager(self) -> ContextManager:
        return self._graph.get("context_manager")

    @property
    def resource_registry(self) -> ResourceRegistry:
        return self._graph.get("resource_registry")

    @property
    def session_log(self) -> "SessionLog":
        return self._graph.get("session_log")

    @property
    def event_bus(self):
        """The unified agent event spine (lazy leaf), the loop's sole producer.

        A pure leaf: reading it constructs a bare :class:`EventBus` with zero
        cross-component reads and zero subscribers. Subscribing the roster is a
        *separate* lifecycle step — :meth:`_wire_spine`, driven explicitly from
        ``Role._ensure_ready`` before the first turn — so no component's
        construction can transitively pull a wired bus and no construction cycle
        can form. Reading it outside the run lifecycle therefore yields an
        *unwired* spine by design; a test inspecting ``subscribers`` calls
        :meth:`_wire_spine` first (the step the runtime performs).
        """
        return self._graph.get("event_bus")

    @property
    def file_snapshot_recorder(self) -> "FileSnapshotRecorder":
        return self._graph.get("file_snapshot_recorder")

    @property
    def terminal_state_recorder(self) -> "TerminalStateRecorder":
        return self._graph.get("terminal_state_recorder")

    @property
    def kernel_state_recorder(self) -> "KernelStateRecorder":
        return self._graph.get("kernel_state_recorder")

    @property
    def browser_state_recorder(self) -> "BrowserStateRecorder":
        return self._graph.get("browser_state_recorder")

    @property
    def hook_manager(self):
        return self._graph.get("hook_manager")

    @property
    def lsp_service(self):
        return self._graph.get("lsp_service")

    @property
    def sandbox_runtime(self):
        return self._graph.get("sandbox_runtime")

    @property
    def diagnostics_buffer(self):
        return self._graph.get("diagnostics_buffer")

    @property
    def file_watch_service(self):
        return self._graph.get("file_watch_service")

    @property
    def turn_context_sources(self) -> list:
        return self._graph.get("turn_context_sources")

    @property
    def turn_context_bus(self):
        return self._graph.get("turn_context_bus")

    def make_loop(self) -> BaseLoop:
        """Build the react-loop strategy for one run() via the graph factory.

        Resolves the (cached) ``loop_factory`` and calls it, yielding a fresh
        loop wired to a fresh think engine — both selected by the schema's
        ``loop_kind`` / ``think_kind`` read at call-time, so a mid-session
        strategy swap takes effect on the next turn.
        """
        return self._graph.get("loop_factory")()

    def make_think_subsystems(self) -> ThinkSubsystems:
        """Build the per-think() subsystem bundle via the graph factory.

        Resolves the (cached) ``think_subsystems_factory`` and calls it, so the
        live collaborators handed to PromptBuilder are read fresh each think()
        (e.g. a component lazy-built after the provider was constructed).
        """
        return self._graph.get("think_subsystems_factory")()

    @property
    def command_channel(self) -> CommandChannel:
        return self._graph.get("command_channel")

    @property
    def context_provider(self) -> ContextProvider:
        return self._graph.get("context_provider")

    # =========================================================================
    # Wiring — runtime cross-references, layered on after construction
    # =========================================================================

    def _wire_spine(self) -> None:
        """Subscribe the declarative roster onto the event bus, exactly once.

        The single place runtime cross-references between the spine and its
        subscribers are established, invoked as an explicit lifecycle step from
        ``Role._ensure_ready`` (never as a hidden side-effect of a component
        read). Split from *construction* on purpose: the ``event_bus`` component
        is a bare leaf, so the build graph is a pure DAG; the cyclic wiring
        (bus ⇄ subscribers) is layered on afterward, here, once everyone is born.
        In ``Role.run`` the bus is bound (``set_bus``) and then wired before the
        first ``emit``, so no event is ever raised onto an unwired spine.

        Idempotent: :attr:`_spine_wired` is set ``True`` *before* the body runs,
        so a redundant call (or a transitive re-entry while building a subscriber)
        short-circuits — the roster is subscribed exactly once.
        """
        if self._spine_wired:
            return
        self._spine_wired = True
        bus = self.event_bus
        for sub in self._build_event_subscribers():
            bus.subscribe(sub)

    def _wire_collaborators(self) -> None:
        """Establish runtime edges *between built collaborators*, exactly once.

        The sibling of :meth:`_wire_spine` for cross-references that are neither
        the event spine nor construction inputs: edges one component holds onto
        *another* at runtime. Kept out of the builders so constructing a component
        never mutates a sibling as a hidden side-effect — every builder stays a
        pure function of its inputs and the build graph a pure DAG.

        Today this is a single edge: the router's COMPRESS-recovery reducer is the
        ContextManager's HARD fold+drop reducer, so every LLM the router
        builds/routes (incl. the main think path via ``route(llm_config=)``) can
        shrink+re-issue an overflowing wire payload. A one-way edge
        (router ← manager) onto two leaves, so order is immaterial; it just needs
        both to exist, which this lifecycle step guarantees.

        Idempotent via :attr:`_collaborators_wired` (set before the body).
        """
        if self._collaborators_wired:
            return
        self._collaborators_wired = True
        self.router.context_reducer = self.context_manager.recovery_reducer

    def _build_event_subscribers(self) -> list:
        """The single declarative roster of every event-bus subscriber.

        One list, read top-to-bottom for humans; subscribe order is immaterial
        (the bus re-sorts each plane by stage/priority). Opt-in subscribers are
        ``None`` when their layer is off and dropped at the end, mirroring the
        turn-context roster. To add a subscriber — infra or feed — add one entry
        here; there are no hand-written ``bus.subscribe`` calls and no back-ref
        special cases (a producer like the LSP service declares ``on_subscribed``
        and the bus hands it its own handle).

        The roster spans the control-plane :class:`HookSubscriber` (when a hook
        layer exists), the always-on infra observers (:class:`RecorderSubscriber`,
        :class:`LogSubscriber`) plus the conditional :class:`TracingSubscriber` /
        :class:`ReporterSubscriber`, the opt-in :class:`LspService` (observer +
        producer), and every dual-role turn-context feed (those exposing
        ``handle`` — an :class:`ObservationSubscriber`) pulled from the single
        :attr:`turn_context_sources` roster so the input edge can never drift out
        of sync with what renders.
        """
        hook_manager = self.hook_manager
        subs = [
            HookSubscriber(hook_manager) if hook_manager is not None else None,
            RecorderSubscriber(self.session_log),
            LogSubscriber(),
            TracingSubscriber(LangfuseBackend(), trace_steps=step_tracing_enabled()) if is_enabled() else None,
            ReporterSubscriber(METAGPT_REPORTER_DEFAULT_URL) if METAGPT_REPORTER_DEFAULT_URL else None,
            self.lsp_service,  # observer + producer (on_subscribed); None when LSP off
        ]
        subs += [s for s in self.turn_context_sources if isinstance(s, ObservationSubscriber)]
        return [s for s in subs if s is not None]

    # =========================================================================
    # Peek accessors — return the raw slot without triggering a build. Used by
    # teardown / turn-boundary paths that must not lazily construct a component.
    # =========================================================================

    def peek_bg_pool(self) -> Optional[BackgroundTaskPool]:
        """Return the background pool only if a tool already created it.

        Never lazily constructs one (unlike the ``bg_pool`` property): the loop
        and ``wait_interruptible`` only ever inspect pending state / await
        completion, so materializing a pool just to peek would be wasteful.
        """
        return self._graph.peek("bg_pool")

    def peek_event_bus(self):
        """The event bus if already built, else ``None`` (no lazy construction)."""
        return self._graph.peek("event_bus")

    def peek_executor(self):
        """The tool executor if already built, else ``None``."""
        return self._graph.peek("executor")

    def peek_lsp_service(self):
        """The LSP service if already built, else ``None``."""
        return self._graph.peek("lsp_service")

    def peek_sandbox_runtime(self):
        """The OS-level sandbox runtime if already built, else ``None``."""
        return self._graph.peek("sandbox_runtime")

    def peek_resource_guard(self):
        """The live resource-cap guard if the sandbox runtime is built, else ``None``.

        Exposes the mutable :class:`ResourceGuard` so an interactive session can
        adjust caps (``set_memory_max`` etc.); the change is read fresh by the
        runtime on the next wrapped command.
        """
        return self._state.resource_guard

    def peek_file_watch_service(self):
        """The file-watch service if already built, else ``None``."""
        return self._graph.peek("file_watch_service")

    def set_task_completion_wake(self, wake: "Optional[Callable]") -> None:
        """Wire a wake callback onto the background task pool.

        Called by the scheduler/REPL after adopting the role so that background
        task completions trigger a new turn instead of waiting for user input.
        The pool is built lazily and may not exist yet, so the callback is also
        stashed in ``state.pending_task_completion_wake`` for the builder to pass
        on creation, and rebinds a live pool.
        """
        self._state.pending_task_completion_wake = wake
        pool = self._graph.peek("bg_pool")
        if pool is not None:
            pool.set_wake(wake)

    # =========================================================================
    # Hook registration
    # =========================================================================

    def register_hook(self, event: str, fn, matcher: Optional[str] = None) -> None:
        """Register an in-process Python hook callback (the SDK-style path).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        manager = self._graph.peek("hook_manager")
        if manager is not None:
            manager.register(event, fn, matcher)
        else:
            self._state.hook_callbacks.append((event, fn, matcher))

    # =========================================================================
    # Turn-context roster helpers (RoleComponents-local, not graph components)
    # =========================================================================

    def _touched_files(self) -> list[str]:
        """Absolute paths this session has read (the record_file_read trajectory).

        Feeds :class:`SkillActivationContextSource` so path-gated skills light up
        when their patterns match a file the agent is working with, and
        :class:`FileRehydrator` so a compaction re-reads the recent working set.
        Best-effort — returns an empty list before any read or if state is
        unavailable.
        """
        try:
            return list(self._role.state._file_read_state.keys())
        except Exception:  # noqa: BLE001 — purely advisory
            return []

    def _read_state(self) -> dict:
        """Snapshot of ``{path: mtime_ns_when_last_read}`` for this session.

        Feeds :class:`ChangedFilesContextSource` so it can compare each tracked
        file's current on-disk mtime against the one recorded when the agent last
        read it. Best-effort — a fresh copy so the source can't mutate live state.
        """
        try:
            return dict(self._role.state._file_read_state)
        except Exception:  # noqa: BLE001 — purely advisory
            return {}

    # =========================================================================
    # File-watch construction helpers (side-effectful — engages the hook layer)
    # =========================================================================

    def _config_source_roots(self) -> list[str]:
        """Discovered config source files to watch (best-effort, may be empty)."""
        try:
            return [str(sf.path) for sf in discover_source_files(Path(self._role.get_cwd()))]
        except Exception as exc:  # noqa: BLE001 — discovery must never break wiring
            logger.warning(f"RoleComponents: config source discovery failed: {exc}")
            return []

    async def _reload_skills_on_change(self, hook_input) -> None:
        """FileChanged handler: atomically re-scan skills (no-op if uninitialized)."""
        mgr = self._graph.peek("skill_manager")
        if mgr is not None and mgr.reload():
            logger.debug("RoleComponents: skills hot-reloaded after a SKILL.md change")

    async def _reload_config_on_change(self, hook_input) -> None:
        """FileChanged handler: reload the layered config into ``role.config``.

        Swaps in a freshly loaded :class:`Config` so components built *after* the
        reload pick it up; collaborators already built this session keep their
        snapshot. Best-effort — a load failure is logged and swallowed.
        """
        try:
            self._role.config = load_config(Path(self._role.get_cwd()), reload=True)
            logger.debug("RoleComponents: config hot-reloaded after a source-file change")
        except Exception as exc:  # noqa: BLE001 — a bad reload must not break the watcher
            logger.warning(f"RoleComponents: config hot-reload failed: {exc}")

    async def _reload_mcp_on_change(self, hook_input) -> None:
        """FileChanged handler: re-init MCP tools after ``mcp_config.json`` changes.

        Mirrors :meth:`_reload_skills_on_change` for the MCP subsystem: asks the
        executor to re-run discovery against the freshly written config file and
        swap its MCP adapters in place (native ``tool_specs`` rebuild on the next
        request). No-op when the executor is not yet built or the role declares
        no MCP servers. Best-effort — a reload failure is logged and swallowed.
        """
        executor = self.peek_executor()
        if executor is None:
            return
        try:
            if await executor.reload_mcp(self._role.role_schema.mcps):
                logger.debug("RoleComponents: MCP hot-reloaded after an mcp_config.json change")
        except Exception as exc:  # noqa: BLE001 — a bad reload must not break the watcher
            logger.warning(f"RoleComponents: MCP hot-reload failed: {exc}")


# =============================================================================
# Component builders — one pure ``build(ctx)`` per registered spec.
#
# A builder is a module-level function of a single :class:`BuildContext`. It
# reads role facts through ``ctx.role`` / mutable extras through ``ctx.state``,
# a sibling *now* through ``ctx.dep(name)`` (an eager, cycle-tracked edge) and a
# sibling *later* through ``ctx.defer(name)`` (a lazy thunk, no build now). It
# returns the constructed value (``None`` = "not applicable", never cached) and
# never mutates a sibling — runtime cross-references are layered on afterward by
# ``RoleComponents._wire_spine`` / ``_wire_collaborators``. The few construction
# helpers that read only the Role (``_skill_source_dirs``) or RoleComponents-
# local state (``ctx.role._components._touched_files`` etc.) are reached through
# those, keeping the sibling surface to the two edge primitives.
# =============================================================================


# --- leaves ------------------------------------------------------------------
def _build_router(ctx) -> LLMRouter:
    return LLMRouter(ctx.role.context)


def _skill_source_dirs(role, cfg) -> list:
    """Layered skill source directories (precedence-as-data, low→high).

    Bundled package skills are the lowest layer; the conventional
    ``~/.agent/skills`` (user) and ``<cwd>/.agent/skills`` (project) dirs and any
    configured ``extra_dirs`` stack above them (later overrides earlier for
    same-named skills).
    """
    dirs: list[Path] = [_BUILTIN_DIR]
    if cfg.include_user_dir:
        dirs.append(Path.home() / ".agent" / "skills")
    if cfg.include_project_dir:
        dirs.append(Path(role.get_cwd()) / ".agent" / "skills")
    dirs.extend(Path(d) for d in cfg.extra_dirs)
    return dirs


def _build_skill_manager(ctx) -> SkillManager:
    cfg = ctx.role.config.role_zero.skills
    # Per-role opt-in: a role that *lists* skills engages the subsystem even when
    # the global master switch is off (``cfg.enabled or bool(skills)``).
    skills = ctx.role.role_schema.skills
    return SkillManager(
        skills=skills,
        enabled=cfg.enabled or bool(skills),
        source_dirs=_skill_source_dirs(ctx.role, cfg),
    )


def _build_bg_pool(ctx) -> BackgroundTaskPool:
    # A disk-output store is required for ``submit(progress=True)`` to install a
    # per-task progress sink: without it, bggraph node-level ``report_progress``
    # events (START / per-node SUCCESS|FAILED / terminal) are silently dropped,
    # so the only notification the agent ever sees is the whole-task completion
    # push from ``_on_done`` — meaning a long-running graph never wakes a Sleep
    # mid-flight on node completions.
    output_store = TaskOutputStore()
    pool = BackgroundTaskPool(
        msg_buffer=ctx.role.state.msg_buffer,
        output_store=output_store,
        wake=ctx.state.pending_task_completion_wake,
    )
    # Wire the disk-cap kill switch so an output that blows the size cap cancels
    # its task (mirrors Claude Code's #killedForSize).
    output_store.set_on_cap(pool.cancel_for_cap)
    return pool


def _build_session_log(ctx) -> "SessionLog":
    role = ctx.role
    log = SessionLog(role.state.session_id)
    log.create(
        SessionMetaEvent(
            session_id=role.state.session_id,
            parent_session_id=role.state.parent_session_id,
            working_dir=role.state.working_dir,
            original_working_dir=role.state.original_working_dir,
            project_root=role.state.project_root,
            model=getattr(role.config.llm, "model", None),
            role_class=f"{type(role).__module__}.{type(role).__qualname__}",
        )
    )
    return log


def _build_command_channel(ctx) -> CommandChannel:
    return make_command_channel(
        ctx.role.role_schema.command_protocol,
        provider=infer_native_tool_provider(ctx.role.config.llm),
    )


# --- opt-in leaves + availability predicates ---------------------------------
def _hook_available(role, state) -> bool:
    """A hook layer exists iff a HookConfig is declared OR a Python callback was
    queued via ``register_hook`` (the SDK-style path)."""
    return role.role_schema.hooks is not None or bool(state.hook_callbacks)


def _build_hook_manager(ctx):
    """Opt-in agent-lifecycle hook runner, seeded with any queued callbacks.

    The cwd accessor is passed so the hook input tracks ``cd``; the session_id
    ties hooks to the durable log. Only built when :func:`_hook_available`.
    """
    manager = HookManager(
        ctx.role.role_schema.hooks,
        session_id=ctx.role.state.session_id,
        get_cwd=ctx.role.get_cwd,
    )
    for event, fn, matcher in ctx.state.hook_callbacks:
        manager.register(event, fn, matcher)
    return manager


def _lsp_available(role, state) -> bool:
    """An LSP layer exists iff an ``LspConfig`` is enabled with ≥1 server."""
    cfg = role.role_schema.lsp
    return cfg is not None and cfg.enabled and bool(cfg.servers)


def _build_lsp_service(ctx):
    """Opt-in language-server diagnostics service, rooted at the project root.

    Only built when :func:`_lsp_available`; the executor's after-edit seam
    short-circuits with zero overhead otherwise.
    """
    cfg = ctx.role.role_schema.lsp
    root = ctx.role.state.project_root or ctx.role.get_cwd()
    return LspService(cfg, root)


def _sandbox_available(role, state) -> bool:
    """An OS-level sandbox exists iff ``permissions.runtime`` is enabled."""
    permissions = role.role_schema.permissions
    cfg = permissions.runtime if permissions is not None else None
    return cfg is not None and cfg.enabled


def _build_sandbox_runtime(ctx):
    """Opt-in OS-level sandbox runtime, or ``None`` when disabled.

    Confines commands with ``bwrap`` (filesystem + pid namespaces), applies
    process hardening, and routes network through a local allowlist proxy.
    Policy source: a fresh :class:`SandboxGuard` derived from the same
    ``permissions.sandbox`` config the logical boundary uses (or a permissive
    default). Stashes the live :class:`ResourceGuard` on ``ctx.state`` so an
    interactive cap adjustment takes effect on the next command.
    """
    permissions = ctx.role.role_schema.permissions
    cfg = permissions.runtime  # guarded by _sandbox_available
    role = ctx.role
    sandbox_cfg = (permissions.sandbox if permissions is not None else None) or SandboxConfig()

    def guard_factory() -> "SandboxGuard":
        return SandboxGuard(sandbox_cfg, get_cwd=role.get_cwd)

    ctx.state.resource_guard = ResourceGuard(cfg)
    return build_runtime(
        cfg,
        get_cwd=role.get_cwd,
        guard_factory=guard_factory,
        resource_guard=ctx.state.resource_guard,
    )


# --- session-log-backed recorders (eager dep: session_log) -------------------
def _build_file_snapshot_recorder(ctx) -> "FileSnapshotRecorder":
    """Before-image file-history sink, sharing the rollout :class:`SessionLog`.

    ``enabled`` follows ``record_file_history``; ``snapshot_backend`` selects the
    store (``"auto"`` picks the git object db inside a code repo, else the plain
    blob store).
    """
    backend = ctx.role.role_schema.snapshot_backend
    if backend == "auto":
        backend = detect_blob_backend(ctx.role.state.working_dir or None)
    return FileSnapshotRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_file_history,
        backend=backend,
    )


def _build_terminal_state_recorder(ctx) -> "TerminalStateRecorder":
    """Persistent-terminal state sink, sharing the rollout :class:`SessionLog`."""
    return TerminalStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_terminal_state,
    )


def _build_kernel_state_recorder(ctx) -> "KernelStateRecorder":
    """Persistent-kernel state sink, sharing the rollout :class:`SessionLog`."""
    return KernelStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_kernel_state,
    )


def _build_browser_state_recorder(ctx) -> "BrowserStateRecorder":
    """Persistent-browser state sink, sharing the rollout :class:`SessionLog`."""
    return BrowserStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_browser_state,
    )


# --- one-sibling-edge nodes --------------------------------------------------
def _build_executor(ctx) -> ToolExecutor:
    all_tools = ctx.role.role_schema.mcps + ctx.role.role_schema.tools
    # Auto-expose the ``Skill`` bridge tool when the skills subsystem is engaged
    # (the single on-demand entry point for invoking project skills) — including
    # the per-role opt-in where a role lists skills with the global switch off.
    # Read from the built manager so the "enabled" decision lives in one place.
    # Mirrors Terminal's "always-on when relevant" wiring: appended, then deduped
    # so an explicit declaration is harmless and order is preserved.
    if ctx.dep("skill_manager").enabled:
        all_tools = all_tools + ["Skill"]
    all_tools = _dedupe_tools(all_tools)
    return ToolExecutor(
        session_id=ctx.role.state.session_id,
        tools=all_tools,
        role=ctx.role,
        permission_config=ctx.role.role_schema.permissions,
        bus=ctx.dep("event_bus"),  # eager: the bus is a pure leaf
        get_bg_pool=ctx.defer("bg_pool"),  # deferred: only pulled on first submit
    )


def _build_context_manager(ctx) -> ContextManager:
    """The stored-conversation store + compaction orchestrator.

    Owns the conversation history, backed by ``RoleState.context`` so it is
    checkpointed, and exposes the get/add/add_batch/delete API the loop/channel/
    think-engine depend on. Orchestrates the compaction pipeline when the history
    nears the context window; summarization runs on the dedicated compression
    model (the router's COMPRESSION task), while the token budget tracks the
    configured main model's context window. Reads siblings eagerly (registry,
    router, bus, executor) — all leaves / earlier-built nodes, so no cycle. The router's
    COMPRESS-recovery reducer edge is *not* stamped here (that is a runtime
    cross-reference, layered on by :meth:`RoleComponents._wire_collaborators`).
    """
    role = ctx.role
    registry = ctx.dep("resource_registry")
    rehydrator = FileRehydrator(role._components._touched_files)
    # Derive the fold/clear-safe tool set from the live executor so compaction
    # tracks whatever tools are actually bound (each tool self-declares via its
    # ``reconstructable`` ClassVar). Cycle-safe: the executor builds before the
    # context_manager and does not depend on it.
    compactable = ctx.dep("executor").reconstructable_tool_names()
    return ContextManager(
        role.state.context,
        llm=ctx.dep("router").route_for_task(COMPRESSION_TASK),
        model=getattr(role.config.llm, "model", None),
        bus=ctx.dep("event_bus"),
        sticky_provider=registry.project,
        rehydrate_provider=rehydrator.project,
        compactable=compactable,
    )


def _build_turn_context_sources(ctx) -> list:
    """The single per-turn ephemeral-context roster (drives both buses).

    Every feed is an :class:`EphemeralContextSource`; the ones also exposing
    ``handle`` (dual-role :class:`ObservationSubscriber`s) are auto-subscribed to
    the event bus by :meth:`RoleComponents._build_event_subscribers` from *this
    same list*, so the input edge can never drift from what renders. Sources
    depending only on ``common`` (git) or duck-typing a live collaborator (token,
    tool catalog) are wired unconditionally and self-suppress; the LSP feed (the
    dual-role :class:`DiagnosticsBuffer`) is present only when an LSP layer is
    configured.
    """
    role = ctx.role
    components = role._components
    sources = [
        ToolCatalogContextSource(
            get_executor=ctx.defer("executor"),
            get_channel=ctx.defer("command_channel"),
        ),
        GitContextSource(get_cwd=lambda: role.state.working_dir or None),
        TokenPressureContextSource(ctx.defer("context_manager")),
        # Reactive post-compaction notice (also subscribes to the event bus to
        # arm itself off PostCompactEvent — dual-role).
        CompactionNoticeContextSource(),
        # Path-gated skills: surfaced per-turn (NOT the steady index) so touching
        # a matching file never busts the cached system prompt. Self-suppresses
        # when skills are disabled or nothing matches.
        SkillActivationContextSource(
            get_pool=lambda: components.skill_manager.pool,
            get_touched_files=components._touched_files,
        ),
        # The steady Skills index (what skills exist), token-capped.
        SkillListingContextSource(
            get_injector=lambda: components.skill_manager.injector,
            max_tokens=role.config.role_zero.max_skill_tokens,
        ),
        # External-edit freshness: flags tracked files changed on disk since the
        # agent last read them. Self-suppresses when nothing changed.
        ChangedFilesContextSource(components._read_state),
        # Local structure map of the touched set (defines / imports / used-by),
        # pushed per turn so the model can target grep/read instead of re-scanning
        # files it already opened. Dual-role: also resets its frontier on
        # PostCompactEvent to re-emit the full map. Self-suppresses with no touched
        # files or nothing structural to say.
        CodeMapContextSource(get_touched_files=components._touched_files),
    ]
    # LSP diagnostics: the buffer is itself the turn-context source (dual-role —
    # also the bus subscriber fed by the LspService), present only when an LSP
    # layer is configured.
    buffer = ctx.dep("diagnostics_buffer")
    if buffer is not None:
        sources.append(buffer)
    return sources


# --- per-turn factories (kind-dispatched; the graph caches only the factory) -
#
# A factory spec's ``build`` returns a *callable*: the graph caches that callable
# (stateless), and each turn the callable reads the ``*_kind`` schema knob and
# builds a fresh instance via a small builder registry. This is the seam a
# future tool uses to swap the loop / think strategy mid-session — the graph's
# "build once, pure DAG" invariant is preserved because only the factory is
# cached, never the per-turn instance. The closed-over ``ctx`` is safe to call
# after the build completes: ``ctx.dep(name)`` re-enters the resolver with an
# empty resolution stack, resolving a cached sibling (no cycle, no re-build).


#: The signature a loop builder must satisfy to enter :data:`_LOOP_BUILDERS`:
#: it receives the :class:`BuildContext` plus the turn's think engine and returns
#: a :class:`BaseLoop`. Naming the shape (rather than a bare ``Callable``) makes a
#: mis-shaped builder — wrong arity, or a return that isn't a ``BaseLoop`` — a
#: type error at the registration site instead of a ``TypeError``/``AttributeError``
#: on the next ``run()``. This is the same import-time-strictness the rest of the
#: assembly enforces; the registry is the one open extension point, so its
#: contract is spelled out here.
LoopBuilder = Callable[["BuildContext", BaseThinkEngine], BaseLoop]
#: The signature a think-engine builder must satisfy to enter
#: :data:`_THINK_BUILDERS`: it receives the :class:`BuildContext` and returns a
#: :class:`BaseThinkEngine`. Same rationale as :data:`LoopBuilder`.
ThinkBuilder = Callable[["BuildContext"], BaseThinkEngine]


def _build_react_loop(ctx: "BuildContext", think_engine: BaseThinkEngine) -> ReActLoop:
    """Construct one ReActLoop, injecting live collaborators + role callables.

    Scatter-injects reusable components and plain callables only — never the
    Role and never a Role-private callback object. The loop pulls its static
    LoopContext from the context_provider (``loop_context()``), so nothing is
    hand-built here beyond the wiring.
    """
    role = ctx.role
    return ReActLoop(
        think_engine=think_engine,
        command_channel=ctx.dep("command_channel"),
        executor=ctx.dep("executor"),
        memory=ctx.dep("context_manager"),
        context_provider=ctx.dep("context_provider"),
        is_active=role._is_active,
        set_active=role._set_active,
        get_bg_pool=role._peek_bg_pool,
        report_think_result=role._report_think_result,
    )


#: Registry: ``loop_kind`` -> a :data:`LoopBuilder`. The value type is the named
#: builder shape, so a mis-shaped entry is rejected here, not at ``run()``.
_LOOP_BUILDERS: dict[str, LoopBuilder] = {"react": _build_react_loop}


def _build_default_think_engine(ctx: "BuildContext") -> BaseThinkEngine:
    """The standard ThinkEngine, memory-backed by the context manager."""
    return ThinkEngine(memory=ctx.dep("context_manager"), config=ctx.role.config)


#: Registry: ``think_kind`` -> a :data:`ThinkBuilder`. Named value type, same
#: registration-time strictness as :data:`_LOOP_BUILDERS`.
_THINK_BUILDERS: dict[str, ThinkBuilder] = {"default": _build_default_think_engine}


def _build_think_engine_factory(ctx: "BuildContext") -> Callable[[], BaseThinkEngine]:
    """Return a zero-arg factory that builds a fresh think engine per turn.

    Reads ``role_schema.think_kind`` at call-time and dispatches through
    :data:`_THINK_BUILDERS` (falling back to ``"default"`` for an unknown kind).
    """
    def make_think_engine() -> BaseThinkEngine:
        kind = ctx.role.role_schema.think_kind
        builder = _THINK_BUILDERS.get(kind) or _THINK_BUILDERS["default"]
        return builder(ctx)

    return make_think_engine


def _build_loop_factory(ctx: "BuildContext") -> Callable[[], BaseLoop]:
    """Return a zero-arg factory that builds a fresh react loop per run().

    Reads ``role_schema.loop_kind`` at call-time and dispatches through
    :data:`_LOOP_BUILDERS` (falling back to ``"react"``), wiring in a fresh
    think engine from the think factory so each run() gets its own machinery.
    """
    def make_loop() -> BaseLoop:
        kind = ctx.role.role_schema.loop_kind
        builder = _LOOP_BUILDERS.get(kind) or _LOOP_BUILDERS["react"]
        think_engine = ctx.dep("think_engine_factory")()
        return builder(ctx, think_engine)

    return make_loop


def _build_think_subsystems_factory(ctx) -> Callable[[], ThinkSubsystems]:
    """Return a zero-arg factory building the per-think() subsystem bundle.

    Reads the live collaborators fresh each think() (so a component lazy-built
    after the provider was constructed is still seen), mirroring the provider's
    old ``_think_subsystems`` hand-assembly.
    """
    def make_think_subsystems() -> ThinkSubsystems:
        role = ctx.role
        return ThinkSubsystems(
            config=role.config,
            model_name=getattr(role.config.llm, "model", "") or "",
            executor=ctx.dep("executor"),
            skill_manager=ctx.dep("skill_manager"),
            turn_context_bus=ctx.dep("turn_context_bus"),
            command_channel=ctx.dep("command_channel"),
        )

    return make_think_subsystems


# --- side-effectful builder (may return None) --------------------------------
def _build_file_watch_service(ctx):
    """Opt-in external-file-change watcher, or ``None`` when disabled.

    Side-effectful: registering the opt-in hot-reload handlers engages the hook
    layer (so the service has a consumer for its ``FileChanged`` events) and
    extends the watched roots. Returns ``None`` when no ``FileWatchConfig`` is
    enabled or no hook layer exists to consume the events. Started in
    ``Role.run`` and stopped in ``Role.cleanup``; subscribes itself to the event
    bus so a tool-driven write isn't echoed back as an external change.
    """
    role = ctx.role
    components = role._components
    cfg = role.role_schema.file_watch
    if cfg is None or not cfg.enabled:
        return None

    roots = list(cfg.roots) or [role.state.project_root or role.get_cwd()]

    # Auto-wire the opt-in hot-reload handlers *before* touching the hook
    # manager: registering them engages the hook layer (so the service has a
    # consumer for its FileChanged events) and extends the watched roots.
    if cfg.reload_skills:
        components.register_hook("FileChanged", components._reload_skills_on_change, r"SKILL\.md$")
        roots.extend(ctx.dep("skill_manager").source_dirs())
    if cfg.reload_config:
        components.register_hook("FileChanged", components._reload_config_on_change, r"config2?\.yaml$")
        roots.extend(components._config_source_roots())
    if cfg.reload_mcp:
        components.register_hook("FileChanged", components._reload_mcp_on_change, r"mcp_config\.json$")
        roots.append(str(mcp_config_path().parent))

    hook_runner = ctx.dep("hook_manager")
    if hook_runner is None:
        return None  # nothing would consume the FileChanged events

    seen: set[str] = set()
    deduped = [r for r in roots if not (r in seen or seen.add(r))]
    return FileWatchService(
        hook_runner,
        deduped,
        ignore=cfg.ignore,
        check_interval=cfg.check_interval,
        bus=ctx.dep("event_bus"),
    )


def _dedupe_tools(tools: list[str]) -> list[str]:
    """Remove duplicate tool names, preserving first-seen order.

    ``Bash`` (one-shot, jam-proof) and ``Terminal`` (persistent PTY) are distinct
    tools and are both kept as declared — no name rewriting happens.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in tools:
        if tool not in seen:
            seen.add(tool)
            deduped.append(tool)
    return deduped


