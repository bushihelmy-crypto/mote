#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleComponents — lazy assembly + ownership of a Role's subsystems.

The Role is pure orchestration; the wiring of its (mostly opt-in, lazily built)
collaborators — the LLM router, tool executor, context manager, event bus,
session log, hook/LSP/file-watch services, the per-turn context bus, etc. —
lives here. The Role keeps the public property surface (``role.executor``,
``role.event_bus`` …) as thin delegators onto this object, so external callers
and tests are unchanged while the construction logic gets a cohesive home.

``RoleComponents`` holds a back-reference to its Role so each builder can read
the static schema / runtime state / injected config it needs (and pass the Role
itself to collaborators that require it, e.g. the ToolExecutor and the
ContextProvider). Slots cache each built component; ``peek_*`` accessors expose
the raw slot (without triggering a build) for teardown paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from metagpt.common.config.loader import load_config
from metagpt.common.config.sources import discover_source_files
from metagpt.common.const import METAGPT_REPORTER_DEFAULT_URL
from metagpt.common.events import EventBus, LogSubscriber
from metagpt.common.hook import HookManager
from metagpt.common.hook.subscriber import HookSubscriber
from metagpt.common.logs import logger
from metagpt.common.observability.langfuse_backend import LangfuseBackend
from metagpt.common.observability.langfuse_integration import (
    is_enabled,
    step_tracing_enabled,
)
from metagpt.common.observability.tracing import TracingSubscriber
from metagpt.common.schema import SandboxConfig
from metagpt.common.utils.report import ReporterSubscriber
from metagpt.context import ContextManager
from metagpt.context.skills.skill_manager import SkillManager
from metagpt.context.skills.skill_pool import _BUILTIN_DIR
from metagpt.context.turn_context import (
    CompactionNoticeContextSource,
    GitContextSource,
    SkillActivationContextSource,
    TokenPressureContextSource,
    TurnContextBus,
)
from metagpt.environment.watching import FileWatchService
from metagpt.executor.permission.sandbox import (
    ResourceGuard,
    SandboxGuard,
    build_runtime,
)
from metagpt.executor.tasks import BackgroundTaskPool, TaskOutputStore
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.parser import (
    CommandChannel,
    infer_native_tool_provider,
    make_command_channel,
)
from metagpt.roles.context_provider import ContextProvider
from metagpt.roles.lsp import DiagnosticsBuffer, LspService
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
from metagpt.think.think_engine import ThinkEngine

if TYPE_CHECKING:
    from metagpt.roles.role import Role
    from metagpt.session import (
        BrowserStateRecorder,
        FileSnapshotRecorder,
        KernelStateRecorder,
        SessionLog,
        TerminalStateRecorder,
    )


class RoleComponents:
    """Owns and lazily builds the Role's collaborators (see module docstring)."""

    def __init__(self, role: "Role"):
        self._role = role

        # Lazy-init component slots
        self._think_engine: Optional[ThinkEngine] = None
        self._executor: Optional[ToolExecutor] = None
        self._skill_mgr: Optional[SkillManager] = None
        self._bg_pool: Optional[BackgroundTaskPool] = None
        self._command_channel: Optional[CommandChannel] = None
        self._context_provider: Optional[ContextProvider] = None
        self._context_manager: Optional[ContextManager] = None
        self._router: Optional[LLMRouter] = None
        self._session_log = None
        self._event_bus = None
        self._file_snapshot_recorder = None
        self._terminal_state_recorder = None
        self._kernel_state_recorder = None
        self._browser_state_recorder = None
        self._hook_manager = None
        self._lsp_service = None
        self._diagnostics_buffer = None
        self._compaction_notice = None
        self._file_watch_service = None
        self._turn_context_bus = None
        self._sandbox_runtime = None
        self._resource_guard = None
        # Wake callback for bg-task completions, set by the scheduler/REPL via
        # ``set_task_completion_wake``. Stored here because that call can land
        # before the background pool is built; the builder passes it to the pool
        # on creation, and ``set_task_completion_wake`` rebinds a live pool.
        self._pending_task_completion_wake: "Optional[Callable]" = None
        # Programmatic Python hook callbacks, seeded into the HookManager when it
        # is built (register_hook appends here before run()).
        self._hook_callbacks: list[tuple[str, Any, Optional[str]]] = []

    # =========================================================================
    # LLM router
    # =========================================================================

    @property
    def router(self) -> LLMRouter:
        """The LLM router bound to this Role's context (lazy-init, cached).

        The Role no longer holds a single LLM. It holds the router and passes it
        down; whoever needs an LLM resolves one through the router on demand (the
        react loop, via the ContextProvider, triggers it per request). Built once
        over the Role's ``context`` so its model registry + instance cache (and
        the FALLBACK-recovery supplier wired onto each provider) stay consistent.
        """
        if self._router is None:
            self._router = LLMRouter(self._role.context)
        return self._router

    # =========================================================================
    # Lazy-init components
    # =========================================================================

    @property
    def skill_manager(self) -> SkillManager:
        if self._skill_mgr is None:
            cfg = self._role.config.role_zero.skills
            self._skill_mgr = SkillManager(
                skills=self._role.role_schema.skills,
                enabled=cfg.enabled,
                source_dirs=self._skill_source_dirs(cfg),
            )
        return self._skill_mgr

    def _skill_source_dirs(self, cfg) -> list:
        """Layered skill source directories (precedence-as-data, low→high).

        Bundled package skills are the lowest layer; the conventional
        ``~/.agent/skills`` (user) and ``<cwd>/.agent/skills`` (project) dirs
        and any configured ``extra_dirs`` stack above them (later overrides
        earlier for same-named skills).
        """
        dirs: list[Path] = [_BUILTIN_DIR]
        if cfg.include_user_dir:
            dirs.append(Path.home() / ".agent" / "skills")
        if cfg.include_project_dir:
            dirs.append(Path(self._role.get_cwd()) / ".agent" / "skills")
        dirs.extend(Path(d) for d in cfg.extra_dirs)
        return dirs

    @property
    def bg_pool(self) -> BackgroundTaskPool:
        if self._bg_pool is None:
            # A disk-output store is required for ``submit(progress=True)`` to
            # install a per-task progress sink: without it, bggraph node-level
            # ``report_progress`` events (START / per-node SUCCESS|FAILED /
            # terminal) are silently dropped, so the only notification the agent
            # ever sees is the whole-task completion push from ``_on_done`` —
            # meaning a long-running graph never wakes a Sleep mid-flight on
            # node completions.
            output_store = TaskOutputStore()
            pool = BackgroundTaskPool(
                msg_buffer=self._role.state.msg_buffer,
                output_store=output_store,
                wake=self._pending_task_completion_wake,
            )
            # Wire the disk-cap kill switch so an output that blows the size cap
            # cancels its task (mirrors Claude Code's #killedForSize).
            output_store.set_on_cap(pool.cancel_for_cap)
            self._bg_pool = pool
        return self._bg_pool

    @property
    def executor(self) -> ToolExecutor:
        if self._executor is None:
            all_tools = self._role.role_schema.mcps + self._role.role_schema.tools
            # Auto-expose the ``Skill`` bridge tool when skills are enabled (the
            # single on-demand entry point for invoking project skills). Mirrors
            # Terminal's "always-on when relevant" wiring: appended, then deduped
            # so an explicit declaration is harmless and order is preserved.
            if self._role.config.role_zero.skills.enabled:
                all_tools = all_tools + ["Skill"]
            all_tools = _dedupe_tools(all_tools)
            self._executor = ToolExecutor(
                session_id=self._role.state.session_id,
                tools=all_tools,
                role=self._role,
                permission_config=self._role.role_schema.permissions,
                bus=self.event_bus,
                get_bg_pool=lambda: self.bg_pool,
            )
        return self._executor

    @property
    def context_manager(self) -> ContextManager:
        """The stored-conversation store + compaction orchestrator (lazy-init).

        Replaces the old ``Memory`` object: it owns the conversation history,
        backed by ``RoleState.context`` so the history is checkpointed, and
        exposes the get/add/add_batch/delete API the loop/channel/think-engine
        depend on. It also orchestrates microcompact + autocompact when the
        history nears the context window. Its autocompact summarization runs on
        the dedicated compression model, obtained via the router's "compression"
        task (configured to claude-sonnet-4-8); the token budget still tracks the
        configured main model's context window.
        """
        if self._context_manager is None:
            self._context_manager = ContextManager(
                self._role.state.context,
                llm=self.router.route_for_task(COMPRESSION_TASK),
                model=getattr(self._role.config.llm, "model", None),
                bus=self.event_bus,
            )
        return self._context_manager

    @property
    def session_log(self) -> "SessionLog":
        """Durable append-only ``rollout.jsonl`` for this session (lazy-init).

        Builds the log and writes the ``session_meta`` first line on first
        access (``create`` no-ops when the log already exists, so restart/resume
        never re-writes metadata). Shared by the event bus's
        :class:`RecorderSubscriber` (which appends message/compaction/turn
        events) and the :attr:`file_snapshot_recorder` (which interleaves
        before-image snapshots into the same rollout).
        """
        if self._session_log is None:
            role = self._role
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
            self._session_log = log
        return self._session_log

    @property
    def event_bus(self):
        """The unified agent event spine (lazy-init), the loop's sole producer.

        One ordered async stream that every cross-cutting concern subscribes to:
        the :class:`RecorderSubscriber` persists message/compaction/turn events
        to :attr:`session_log` (always wired — recording is always on), and the
        :class:`HookSubscriber` translates control events (UserPromptSubmit /
        Pre|PostToolUse / Pre|PostCompact / SessionStart / Stop) into
        ``HookManager.fire`` calls (wired only when a hook layer exists, so the
        no-hooks path keeps zero overhead). The :class:`LogSubscriber` emits one
        concise log line per semantic event (event-level trace complementing the
        method-level ``@log_class``). The :class:`LspService` (opt-in) subscribes
        to ``FileMutatedEvent`` to sync edited docs + collect diagnostics.

        The bus classifies each subscriber by capability, not by this call order:
        the :class:`HookSubscriber` exposes ``handle_control`` so it lands on the
        **control plane** (phase 1, awaited inline, folds a veto *before* any
        observer runs); every other subscriber is an **observer** (phase 2,
        fanned out, return ignored). Within each plane subscribers run in
        ascending ``priority`` (the recorder before the logger).
        """
        if self._event_bus is None:
            self._event_bus = self._build_event_bus()
        return self._event_bus

    def _build_event_bus(self):
        """Construct the event bus and wire every (opt-in) subscriber onto it.

        Split out of the :attr:`event_bus` property so the property stays a thin
        lazy-cache and the (conditional, multi-subscriber) wiring has an explicit
        home. See the property docstring for the subscriber roster.
        """

        bus = EventBus()
        hook_manager = self.hook_manager
        if hook_manager is not None:
            bus.subscribe(HookSubscriber(hook_manager))
        bus.subscribe(RecorderSubscriber(self.session_log))
        bus.subscribe(LogSubscriber())
        # Tracing rides the same spine: when enabled, a backend-agnostic
        # TracingSubscriber consumes the span + LLM request/response/error
        # events and rebuilds the trace tree from their explicit IDs, driving a
        # pluggable TracerBackend (langfuse today). Dormant — and not even
        # imported — when disabled.

        if is_enabled():
            bus.subscribe(TracingSubscriber(LangfuseBackend(), trace_steps=step_tracing_enabled()))
        # The compaction-notice feed catches PostCompactEvent here (input
        # edge) and replays it as a one-shot turn-context block (output edge,
        # via turn_context_bus). Always wired — it self-suppresses until a
        # compaction fires.
        bus.subscribe(self.compaction_notice)
        # ResourceReporter pushes (non-streaming UI observations) ride the
        # bus too: emit -> ReporterSubscriber POSTs. Wired only when a report
        # endpoint is configured (METAGPT_REPORTER_URL); empty = dormant.

        if METAGPT_REPORTER_DEFAULT_URL:
            bus.subscribe(ReporterSubscriber(METAGPT_REPORTER_DEFAULT_URL))
        # The LSP feed rides the bus on both edges (opt-in: only wired when
        # an LSP layer is configured). Input: the service subscribes to
        # FileMutatedEvent (a tool write) to sync the doc + collect
        # diagnostics, then broadcasts them as a DiagnosticsEvent (so it
        # needs the bus to emit on). Output: the buffer subscribes to those
        # DiagnosticsEvents and stages them for next-turn context.
        lsp = self.lsp_service
        if lsp is not None:
            lsp.bus = bus
            bus.subscribe(lsp)
            buffer = self.diagnostics_buffer
            if buffer is not None:
                bus.subscribe(buffer)
        return bus

    @property
    def file_snapshot_recorder(self) -> "FileSnapshotRecorder":
        """Before-image file-history sink (lazy-init), shared with the rollout log.

        Reuses the same :class:`SessionLog` as :attr:`session_log` so file
        snapshots interleave with the session's other events; the blob store
        lives alongside ``rollout.jsonl``. ``enabled`` follows the schema flag
        ``record_file_history`` (default True) so snapshotting can be turned off
        per role. ``snapshot_backend`` selects the store: ``"auto"`` (default)
        picks the git object db when the working dir is inside a code repo, else
        the plain blob store.
        """
        if self._file_snapshot_recorder is None:
            backend = self._role.role_schema.snapshot_backend
            if backend == "auto":
                backend = detect_blob_backend(self._role.state.working_dir or None)

            self._file_snapshot_recorder = FileSnapshotRecorder(
                self.session_log,
                enabled=self._role.role_schema.record_file_history,
                backend=backend,
            )
        return self._file_snapshot_recorder

    @property
    def terminal_state_recorder(self) -> "TerminalStateRecorder":
        """Persistent-terminal state sink (lazy-init), shared with the rollout log.

        Reuses the same :class:`SessionLog` as :attr:`session_log` so the
        terminal-state event interleaves with the session's other events.
        ``enabled`` follows the schema flag ``record_terminal_state`` (default
        True) so it can be turned off per role.
        """
        if self._terminal_state_recorder is None:
            self._terminal_state_recorder = TerminalStateRecorder(
                self.session_log,
                enabled=self._role.role_schema.record_terminal_state,
            )
        return self._terminal_state_recorder

    @property
    def kernel_state_recorder(self) -> "KernelStateRecorder":
        """Persistent-kernel state sink (lazy-init), shared with the rollout log.

        The Python sibling of :attr:`terminal_state_recorder`. Reuses the same
        :class:`SessionLog` as :attr:`session_log` so the kernel-state event
        interleaves with the session's other events. ``enabled`` follows the
        schema flag ``record_kernel_state`` (default True) so it can be turned
        off per role.
        """
        if self._kernel_state_recorder is None:
            self._kernel_state_recorder = KernelStateRecorder(
                self.session_log,
                enabled=self._role.role_schema.record_kernel_state,
            )
        return self._kernel_state_recorder

    @property
    def browser_state_recorder(self) -> "BrowserStateRecorder":
        """Persistent-browser state sink (lazy-init), shared with the rollout log.

        The browser sibling of :attr:`terminal_state_recorder`. Reuses the same
        :class:`SessionLog` as :attr:`session_log` so the browser-state event
        interleaves with the session's other events. ``enabled`` follows the
        schema flag ``record_browser_state`` (default True) so it can be turned
        off per role — relevant here because ``storage_state`` may carry session
        cookies.
        """
        if self._browser_state_recorder is None:
            self._browser_state_recorder = BrowserStateRecorder(
                self.session_log,
                enabled=self._role.role_schema.record_browser_state,
            )
        return self._browser_state_recorder

    @property
    def hook_manager(self):
        """Opt-in agent-lifecycle hook runner (lazy-init), or ``None``.

        Built only when a ``HookConfig`` is declared on the schema OR a Python
        callback was registered via :meth:`register_hook`. When neither exists it
        stays ``None`` so every call site short-circuits with zero overhead —
        the same opt-in model as the permission engine. The cwd accessor is
        passed so the hook input tracks ``cd``; the session_id ties hooks to the
        durable log.
        """
        if self._hook_manager is None:
            if self._role.role_schema.hooks is None and not self._hook_callbacks:
                return None

            self._hook_manager = HookManager(
                self._role.role_schema.hooks,
                session_id=self._role.state.session_id,
                get_cwd=self._role.get_cwd,
            )
            for event, fn, matcher in self._hook_callbacks:
                self._hook_manager.register(event, fn, matcher)
        return self._hook_manager

    @property
    def lsp_service(self):
        """Opt-in language-server diagnostics service (lazy-init), or ``None``.

        Built only when an ``LspConfig`` with ``enabled=True`` and at least one
        server is declared on the schema; otherwise stays ``None`` so the
        executor's after-edit seam short-circuits with zero overhead (same
        opt-in model as the hook/permission layers). Rooted at the project root
        so language servers resolve imports against the right workspace.
        """
        if self._lsp_service is None:
            cfg = self._role.role_schema.lsp
            if cfg is None or not cfg.enabled or not cfg.servers:
                return None

            root = self._role.state.project_root or self._role.get_cwd()
            self._lsp_service = LspService(cfg, root)
        return self._lsp_service

    @property
    def sandbox_runtime(self):
        """Opt-in OS-level sandbox runtime (lazy-init), or ``None``.

        Built only when ``permissions.runtime`` is declared with
        ``enabled=True``; otherwise stays ``None`` so the command-execution
        tools (Bash / terminal / python) run un-sandboxed exactly as before
        (same opt-in model as the LSP / hook layers). The runtime confines
        commands with ``bwrap`` (filesystem + pid namespaces), applies process
        hardening, and routes network through a local allowlist proxy.

        Policy source: a fresh :class:`SandboxGuard` derived from the same
        ``permissions.sandbox`` config the logical boundary uses (or a
        permissive default when none is declared), so OS-level writable roots
        track the logical ones.
        """
        if self._sandbox_runtime is None:
            permissions = self._role.role_schema.permissions
            cfg = permissions.runtime if permissions is not None else None
            if cfg is None or not cfg.enabled:
                return None

            role = self._role
            sandbox_cfg = (permissions.sandbox if permissions is not None else None) or SandboxConfig()

            def guard_factory() -> "SandboxGuard":
                return SandboxGuard(sandbox_cfg, get_cwd=role.get_cwd)

            # Hold the live resource-cap guard so an interactive session
            # adjustment (e.g. raising the memory cap) takes effect on the next
            # command — mirroring how the SandboxGuard backs the policy provider.
            self._resource_guard = ResourceGuard(cfg)
            self._sandbox_runtime = build_runtime(
                cfg,
                get_cwd=role.get_cwd,
                guard_factory=guard_factory,
                resource_guard=self._resource_guard,
            )
        return self._sandbox_runtime

    @property
    def diagnostics_buffer(self):
        """Output-side LSP diagnostics feed (lazy-init), or ``None``.

        A dual-role object: the bus consumer that accumulates the
        :class:`DiagnosticsEvent`\\s the :attr:`lsp_service` broadcasts, *and*
        the per-turn ``EphemeralContextSource`` that renders them at the turn
        boundary (so it goes straight into :attr:`turn_context_bus` — no
        separate wrapper). Gated on the same ``LspConfig`` as the service (both
        halves of the LSP feed appear together or not at all).
        """
        if self._diagnostics_buffer is None:
            cfg = self._role.role_schema.lsp
            if cfg is None or not cfg.enabled or not cfg.servers:
                return None

            self._diagnostics_buffer = DiagnosticsBuffer()
        return self._diagnostics_buffer

    @property
    def compaction_notice(self):
        """One-shot "history was compacted" feed (lazy-init), always wired.

        A single object playing both sides of a push→pull bridge: it subscribes
        to :attr:`event_bus` to catch the ``PostCompactEvent`` the
        ``ContextManager`` emits after an automatic compaction, then surfaces a
        one-shot reminder through the :attr:`turn_context_bus` on the next
        think() cycle. Cheap and self-suppressing (silent until a compaction
        actually happens), so it is wired unconditionally like git/token.
        """
        if self._compaction_notice is None:
            self._compaction_notice = CompactionNoticeContextSource()
        return self._compaction_notice

    @property
    def file_watch_service(self):
        """Opt-in external-file-change watcher (lazy-init), or ``None``.

        Built only when a ``FileWatchConfig`` with ``enabled=True`` is declared
        AND a hook layer exists to consume the ``FileChanged`` events it fires;
        otherwise stays ``None`` so there's zero overhead (same opt-in model as
        the LSP/hook layers). ``roots`` default to the project root (or cwd).

        The service subscribes itself to :attr:`event_bus`, so a tool-driven
        write (surfaced as a :class:`FileMutatedEvent`) is recorded as a
        self-write and the next poll doesn't echo the agent's own edit back as
        an external change. Started in ``Role.run`` and stopped in
        ``Role.cleanup``.
        """
        if self._file_watch_service is None:
            self._file_watch_service = self._build_file_watch_service()
        return self._file_watch_service

    def _build_file_watch_service(self):
        """Construct the file-watch service, or return ``None`` when disabled.

        Split out of the :attr:`file_watch_service` property because building it
        is *side-effectful* — it registers the opt-in hot-reload hooks (which
        engages the hook layer) and extends the watched roots — so it reads as a
        deliberate construction step rather than a transparent attribute read.
        """
        cfg = self._role.role_schema.file_watch
        if cfg is None or not cfg.enabled:
            return None

        roots = list(cfg.roots) or [self._role.state.project_root or self._role.get_cwd()]

        # Auto-wire the opt-in hot-reload handlers *before* touching the hook
        # manager: registering them engages the hook layer (so the service
        # has a consumer for its FileChanged events) and extends the watched
        # roots to the relevant source dirs/files.
        if cfg.reload_skills:
            self.register_hook("FileChanged", self._reload_skills_on_change, r"SKILL\.md$")
            roots.extend(self.skill_manager.source_dirs())
        if cfg.reload_config:
            self.register_hook("FileChanged", self._reload_config_on_change, r"config2?\.yaml$")
            roots.extend(self._config_source_roots())

        hook_runner = self.hook_manager
        if hook_runner is None:
            return None  # nothing would consume the FileChanged events

        seen: set[str] = set()
        deduped = [r for r in roots if not (r in seen or seen.add(r))]
        return FileWatchService(
            hook_runner,
            deduped,
            ignore=cfg.ignore,
            check_interval=cfg.check_interval,
            bus=self.event_bus,
        )

    def _config_source_roots(self) -> list[str]:
        """Discovered config source files to watch (best-effort, may be empty)."""
        try:
            return [str(sf.path) for sf in discover_source_files(Path(self._role.get_cwd()))]
        except Exception as exc:  # noqa: BLE001 — discovery must never break wiring
            logger.warning(f"RoleComponents: config source discovery failed: {exc}")
            return []

    async def _reload_skills_on_change(self, hook_input) -> None:
        """FileChanged handler: atomically re-scan skills (no-op if uninitialized)."""
        mgr = self._skill_mgr
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

    @property
    def turn_context_bus(self):
        """The per-turn ephemeral-context bus (lazy-init), always present.

        Assembles the volatile per-cycle feeds that land in the user prompt's
        ``<system-reminder>`` (never the cacheable system prompt, never stored
        in history): git working-tree status, token-pressure notes, the
        post-compaction notice, background task progress, and LSP diagnostics.
        Each feed is an :class:`~metagpt.common.interface.EphemeralContextSource`;
        the bus renders them per think() cycle and merges the non-empty blocks
        (it sorts by ``priority``, so append order here is immaterial).

        Sources that depend only on ``common`` (git) or duck-type a live
        collaborator (token) are wired unconditionally — they self-suppress
        (return ``None``) when there's nothing to report (off-repo, no token
        pressure). The LSP feed is the :class:`DiagnosticsBuffer` itself (a
        dual-role bus-subscriber + context source), wired only when an LSP layer
        is configured.

        Background-task progress is deliberately NOT a turn-context source: the
        progress writer / pool already push structured notifications directly
        into ``msg_buffer`` (graph start/end, per-node success/route/failure),
        so an additional ``<task-attachment>`` feed every cycle would
        double-report the same progress.
        """
        if self._turn_context_bus is None:
            sources = [
                GitContextSource(),
                TokenPressureContextSource(self.context_manager),
                # Reactive post-compaction notice (same instance subscribes to
                # the event bus to arm itself off PostCompactEvent).
                self.compaction_notice,
                # Path-gated skills: surfaced per-turn (NOT the steady index) so
                # touching a matching file never busts the cached system prompt.
                # Self-suppresses when skills are disabled or nothing matches.
                SkillActivationContextSource(
                    get_pool=lambda: self.skill_manager.pool,
                    get_touched_files=self._touched_files,
                ),
            ]
            # LSP diagnostics: the buffer is itself the turn-context source
            # (dual-role — it's also the bus subscriber fed by the LspService),
            # present only when an LSP layer is configured.
            buffer = self.diagnostics_buffer
            if buffer is not None:
                sources.append(buffer)
            self._turn_context_bus = TurnContextBus(sources)
        return self._turn_context_bus

    def _touched_files(self) -> list[str]:
        """Absolute paths this session has read (the record_file_read trajectory).

        Feeds :class:`SkillActivationContextSource` so path-gated skills light up
        when their patterns match a file the agent is working with. Best-effort —
        returns an empty list before any read or if state is unavailable.
        """
        try:
            return list(self._role.state._file_read_state.keys())
        except Exception:  # noqa: BLE001 — purely advisory
            return []

    @property
    def think_engine(self) -> ThinkEngine:
        if self._think_engine is None:
            self._think_engine = ThinkEngine(
                memory=self.context_manager,
                config=self._role.config,
            )
        return self._think_engine

    @property
    def command_channel(self) -> CommandChannel:
        """The protocol strategy (XML vs native tool-use) for this Role.

        Built once from RoleSchema.command_protocol; owns how commands are
        prompted, called, and parsed so the react loop stays protocol-agnostic.
        The native tool-spec envelope is inferred from the LLM config (it must
        match the client that issues the request), not set on the schema.
        """
        if self._command_channel is None:
            self._command_channel = make_command_channel(
                self._role.role_schema.command_protocol,
                provider=infer_native_tool_provider(self._role.config.llm),
            )
        return self._command_channel

    @property
    def context_provider(self) -> ContextProvider:
        """The per-flow parameter packer for this Role (lazy-init).

        Holds the Role and reads it to pack what each react flow needs: the
        think request (prepare()) and the static observe + loop-control bundle
        (loop_context()). The react loop only sees the narrow
        BaseContextProvider face, never the Role, so role behavior stays in the
        Role. The provider only reads the Role; it never writes RoleState and
        never lazy-inits components (ownership stays here on the Role).
        """
        if self._context_provider is None:
            self._context_provider = ContextProvider(self._role)
        return self._context_provider

    # =========================================================================
    # Peek accessors — return the raw slot without triggering a build. Used by
    # teardown / turn-boundary paths that must not lazily construct a component.
    # =========================================================================

    def peek_bg_pool(self) -> Optional[BackgroundTaskPool]:
        """Return the background pool only if a tool already created it.

        Never lazily constructs one (unlike the ``bg_pool`` property): the loop
        and ``wait_interruptible`` only ever inspect pending state / await
        completion, so materializing a pool just to peek would be wasteful. A
        named accessor instead of reading the ``_bg_pool`` slot directly so the
        "peek, don't create" intent is explicit at every call site.
        """
        return self._bg_pool

    def peek_event_bus(self):
        """The event bus if already built, else ``None`` (no lazy construction)."""
        return self._event_bus

    def peek_executor(self):
        """The tool executor if already built, else ``None``."""
        return self._executor

    def peek_lsp_service(self):
        """The LSP service if already built, else ``None``."""
        return self._lsp_service

    def peek_sandbox_runtime(self):
        """The OS-level sandbox runtime if already built, else ``None``."""
        return self._sandbox_runtime

    def peek_resource_guard(self):
        """The live resource-cap guard if the sandbox runtime is built, else ``None``.

        Exposes the mutable :class:`ResourceGuard` so an interactive session can
        adjust caps (``set_memory_max`` etc.); the change is read fresh by the
        runtime on the next wrapped command.
        """
        return self._resource_guard

    def peek_file_watch_service(self):
        """The file-watch service if already built, else ``None``."""
        return self._file_watch_service

    def set_task_completion_wake(self, wake: "Optional[Callable]") -> None:
        """Wire a wake callback onto the background task pool.

        Called by the scheduler/REPL after adopting the role so that background
        task completions trigger a new turn instead of waiting for user input.
        The pool is built lazily and may not exist yet, so the callback is also
        stashed in ``_pending_task_completion_wake`` for the builder to pass on
        creation.
        """
        self._pending_task_completion_wake = wake
        if self._bg_pool is not None:
            self._bg_pool.set_wake(wake)

    # =========================================================================
    # Hook registration
    # =========================================================================

    def register_hook(self, event: str, fn, matcher: Optional[str] = None) -> None:
        """Register an in-process Python hook callback (the SDK-style path).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        if self._hook_manager is not None:
            self._hook_manager.register(event, fn, matcher)
        else:
            self._hook_callbacks.append((event, fn, matcher))


def _dedupe_tools(tools: list[str]) -> list[str]:
    """Remove duplicate tool names, preserving first-seen order.

    ``Bash`` (one-shot, jam-proof) and ``Terminal`` (persistent PTY) are
    distinct tools and are both kept as declared — no name rewriting happens.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in tools:
        if tool not in seen:
            seen.add(tool)
            deduped.append(tool)
    return deduped
