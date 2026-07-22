#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Generic, Optional, Set, TypeVar, cast
from uuid import uuid4

from mote.common.base import BaseRole
from mote.common.const import MESSAGE_ROUTE_TO_SELF
from mote.common.disk import get_disk_writer, mtime_ns
from mote.common.events import (
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    SessionStartEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
    set_bus,
    span,
)
from mote.common.exception import RoleContextNotSetError
from mote.common.interface import RunLeaseCoordinator
from mote.common.logs import bind_session_logfile, bind_trace, log_class, logger, unbind_session_logfile
from mote.common.schema import AIMessage, CauseBy, Message, RunLeasePolicy, RunResult, TranscriptRef
from mote.common.utils.common import any_to_str, role_raise_decorator
from mote.context import ContextManager
from mote.context.skills.skill_manager import SkillManager
from mote.executor.tasks import BackgroundTaskPool
from mote.executor.tool_executor import ToolExecutor
from mote.parser import CommandChannel
from mote.roles.context_provider import ContextProvider
from mote.roles.output_contract import OutputContract, text_output_contract
from mote.roles.role_components import RoleComponents
from mote.roles.role_schema import RoleSchema
from mote.roles.role_state import RoleState
from mote.router.router import LLMRouter
from mote.session import list_sessions as _list
from mote.session.attribution import HunkAttribution
from mote.session.events import SessionMetaEvent
from mote.session.hunk_ops import HunkOps

OutputT = TypeVar("OutputT")

if TYPE_CHECKING:
    from mote.common.schema import AskUserQuestionAnswers, AskUserQuestionItem, DeviceConfig
    from mote.common.schema.permission_types import ApprovalChoice, ApprovalRequest
    from mote.context.skills.skill_pool import SkillPool
    from mote.executor.capability_types import CapabilityMap
    from mote.executor.dependency.browser_profile import BrowserProfileStore
    from mote.executor.tool_result import ToolResult
    from mote.router.llm.llm_response import WebSearchHit
    from mote.sandbox import SandboxRuntime
    from mote.session import (
        BrowserStateRecorder,
        FileSnapshotRecorder,
        KernelStateRecorder,
        SessionLog,
        TerminalStateRecorder,
    )


@log_class(
    level="DEBUG",
    exclude={
        # Hot / trivial accessors and signal setters — wrapping them only adds
        # noise. `run` is excluded here and traced explicitly below (bind_trace).
        "run",
        "get_cwd",
        "set_cwd",
        "record_file_read",
        "get_file_read_mtime",
        "is_resource_visible",
        "put_message",
        "publish_message",
        "tool_capabilities",
        "deactivate",
        "get_memories",
        "set_env",
        "set_addresses",
    },
)
class Role(BaseRole, Generic[OutputT]):
    """Unified Role/Agent — pure orchestration via composition.

    Composes:
      - role_schema: RoleSchema (static config, deploy-time)
      - state: RoleState (runtime snapshot, serializable for checkpoint/recovery)
      - Lazy-init components: ThinkEngine, ToolExecutor, SkillManager, etc.

    Not a Pydantic BaseModel: construction is explicit via __init__.
    Serialization is handled by dump()/load() which delegate to RoleState (Pydantic).
    """

    def __init__(
        self,
        *,
        context=None,
        config=None,
        name: Optional[str] = None,
        role_schema: Optional[RoleSchema] = None,
        state: Optional[RoleState] = None,
        output_contract: Optional[OutputContract[OutputT]] = None,
        run_lease_coordinator: RunLeaseCoordinator | None = None,
        run_lease_policy: RunLeasePolicy = RunLeasePolicy(),
        **schema_kwargs,
    ):
        # Static config
        if role_schema is not None:
            self.role_schema = role_schema
        elif schema_kwargs:
            self.role_schema = RoleSchema(**schema_kwargs)
        else:
            self.role_schema = RoleSchema()

        if name is not None:
            self.role_schema.name = name

        # Runtime state
        self.state = state if state is not None else RoleState()

        # External dependencies (injected)
        self._context = context
        self._config = config
        self.output_contract = output_contract or cast(OutputContract[OutputT], text_output_contract())
        self._run_lease_coordinator = run_lease_coordinator
        self._run_lease_policy = run_lease_policy

        # Lazy assembly + ownership of all subsystems (router, executor, context
        # manager, event bus, session log, hook/LSP/file-watch services, the
        # per-turn context bus, …) — including the two behaviour holders (the
        # state controller and the tool-capabilities holder). The Role keeps a
        # thin property surface that delegates onto this holder; the wiring logic
        # lives there. Role.__init__ constructs nothing but this holder.
        self._components = RoleComponents(self)
        # Guards firing SessionStart exactly once across this Role's run() calls.
        self._session_started = False

        # Post-init
        self._init_addresses()

    def __hash__(self):
        return id(self)

    # =========================================================================
    # Serialization — delegates to RoleState (Pydantic) + class registry
    # =========================================================================

    def dump(self) -> dict[str, Any]:
        """Serialize role for checkpoint/recovery."""
        return {
            "__module_class_name": f"{type(self).__module__}.{type(self).__qualname__}",
            "state": self.state.model_dump(),
            "role_schema": self.role_schema.model_dump(),
        }

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Role":
        """Reconstruct a Role from serialized data."""
        schema_data = data.get("role_schema", {})
        state_data = data.get("state", {})
        role_schema = RoleSchema.model_validate(schema_data)
        state = RoleState.model_validate(state_data)
        return cls(role_schema=role_schema, state=state)

    # =========================================================================
    # Properties — context / config / llm delegation
    # =========================================================================

    @property
    def name(self) -> str:
        return self.role_schema.name

    @name.setter
    def name(self, value: str):
        self.role_schema.name = value

    @property
    def components(self) -> RoleComponents:
        """The Role's subsystem holder (lazy assembly + ownership). See
        :class:`RoleComponents`. The component properties below delegate here.
        """
        return self._components

    @property
    def _state_ctl(self):
        """Behaviour over the (pure-DTO) RoleState — resolved through the graph.

        The Role's state methods (cwd, file-read map, active signal, …) are thin
        delegators onto this controller; it lives in the component graph like
        every other collaborator so ``__init__`` builds nothing itself.
        """
        return self._components.state_ctl

    @property
    def _capabilities(self):
        """Subsystem-backed tool capabilities (human I/O, sleep, end-of-session
        summary, skill forks, the task/skill pools) — resolved through the graph.
        """
        return self._components.capabilities

    @property
    def _session_manager(self):
        """Session resume/fork behaviour (replay history, branch a sibling) —
        resolved through the graph. The Role keeps thin ``resume_session`` /
        ``fork_session`` delegators onto this holder.
        """
        return self._components.session_manager

    @property
    def router(self) -> LLMRouter:
        """The LLM router bound to this Role's context (delegates to components)."""
        return self._components.router

    @property
    def config(self):
        if self._config:
            return self._config
        return self.context.config

    @config.setter
    def config(self, config):
        self._config = config

    @property
    def context(self):
        if self._context:
            return self._context
        raise RoleContextNotSetError("Role.context not set. Pass context= when constructing the Role.")

    @context.setter
    def context(self, context):
        self._context = context

    # =========================================================================
    # Component properties (lazy-init)
    # =========================================================================

    # Each property below is a thin delegator onto :class:`RoleComponents`,
    # which owns the slots + lazy construction. External callers and tests keep
    # using ``role.<component>``; the wiring lives in role_components.py.

    @property
    def skill_manager(self) -> SkillManager:
        return self._components.skill_manager

    @property
    def bg_pool(self) -> BackgroundTaskPool:
        return self._components.bg_pool

    def _peek_bg_pool(self) -> Optional[BackgroundTaskPool]:
        """Return the background pool only if a tool already created it (no build)."""
        return self._components.peek_bg_pool()

    def set_task_completion_wake(self, wake) -> None:
        """Wire a wake callback so bg-task completions trigger a new turn."""
        self._components.set_task_completion_wake(wake)

    @property
    def executor(self) -> ToolExecutor:
        return self._components.executor

    @property
    def context_manager(self) -> ContextManager:
        return self._components.context_manager

    @property
    def session_log(self) -> "SessionLog":
        return self._components.session_log

    @property
    def event_bus(self):
        return self._components.event_bus

    @property
    def file_snapshot_recorder(self) -> "FileSnapshotRecorder":
        return self._components.file_snapshot_recorder

    @property
    def hunk_ledger(self):
        """The session's durable change-attribution ledger (always present)."""
        return self._components.hunk_ledger

    @property
    def hunk_ops(self) -> "HunkOps":
        """Accept / reject / undo engine over the session's hunk ledger.

        Coordinates the ledger, the blob store, and this Role's baseline +
        read-state maps (reached through narrow callbacks so the session-layer
        engine stays roles-independent). Built once and cached — the ledger and
        the state controller it binds to are stable for the Role's lifetime.
        """
        ops = getattr(self, "_hunk_ops", None)
        if ops is None:

            def _refresh_read_state(path: str) -> None:
                m = mtime_ns(path)
                if m is not None:
                    self._state_ctl.record_file_read(path, m)

            ops = HunkOps(
                self.hunk_ledger,
                self.file_snapshot_recorder.blobs,
                set_baseline=self._state_ctl.record_file_baseline,
                refresh_read_state=_refresh_read_state,
            )
            self._hunk_ops = ops
        return ops

    @property
    def hunk_attribution(self) -> "HunkAttribution":
        """Read-side projection of the hunk ledger for review UIs.

        Groups the ledger's records (by turn / file / source) and rehydrates
        each hunk's before/after text on demand from the blob store. Built once
        and cached — the ledger and blob store it binds to are stable for the
        Role's lifetime.
        """
        attr = getattr(self, "_hunk_attribution", None)
        if attr is None:
            attr = HunkAttribution(self.hunk_ledger, self.file_snapshot_recorder.blobs)
            self._hunk_attribution = attr
        return attr

    @property
    def resource_registry(self):
        return self._components.resource_registry

    @property
    def terminal_state_recorder(self) -> "TerminalStateRecorder":
        return self._components.terminal_state_recorder

    @property
    def kernel_state_recorder(self) -> "KernelStateRecorder":
        return self._components.kernel_state_recorder

    @property
    def browser_state_recorder(self) -> "BrowserStateRecorder":
        return self._components.browser_state_recorder

    @property
    def browser_profile_store(self) -> "BrowserProfileStore":
        return self._components.browser_profile_store

    @property
    def hook_manager(self):
        return self._components.hook_manager

    @property
    def lsp_service(self):
        return self._components.lsp_service

    @property
    def sandbox_runtime(self):
        return self._components.sandbox_runtime

    @property
    def diagnostics_buffer(self):
        return self._components.diagnostics_buffer

    def turn_context_source(self, name: str):
        """Look up a per-turn context feed by its ``name`` (or ``None``).

        A generic accessor over the single source roster, replacing the former
        per-feed properties (``compaction_notice`` etc.): adding a feed never
        needs a matching accessor here.
        """
        return next(
            (s for s in self._components.turn_context_sources if s.name == name),
            None,
        )

    @property
    def file_watch_service(self):
        return self._components.file_watch_service

    @property
    def turn_context_bus(self):
        return self._components.turn_context_bus

    def register_hook(self, event: str, fn, matcher: Optional[str] = None) -> None:
        """Register an in-process Python hook callback (delegates to components).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        self._components.register_hook(event, fn, matcher)

    def _report_think_result(self, result) -> None:
        """Publish this turn's think result to state (used by the loop).

        The loop calls this the moment the think task drains, so a tool running
        later in the same turn (e.g. ``end_session``) reads the fresh result off
        RoleState instead of the think-engine machinery — which lets the engine
        be a stateless per-turn factory built by the graph's loop factory.
        """
        self._state_ctl.set_last_think_result(result)

    @property
    def command_channel(self) -> CommandChannel:
        return self._components.command_channel

    @property
    def context_provider(self) -> ContextProvider:
        return self._components.context_provider

    # =========================================================================
    # Framework properties
    # =========================================================================

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def env(self):
        return self.state.env

    @property
    def is_idle(self) -> bool:
        """A role is idle when its message buffer is empty."""
        return self._state_ctl.is_idle

    def set_env(self, env):
        """Set the environment this role belongs to and register addresses."""
        self.state.env = env
        if env:
            env.set_addresses(self, self.state.addresses)

    # =========================================================================
    # Initialization helpers
    # =========================================================================

    def _init_addresses(self):
        """Set default addresses and recovery state."""
        if not self.state.addresses:
            self.state.addresses = (
                {any_to_str(self), self.role_schema.name} if self.role_schema.name else {any_to_str(self)}
            )
        if self.state.latest_observed_msg:
            self.state.recovered = True

    # =========================================================================
    # Framework methods
    # =========================================================================

    def set_addresses(self, addresses: Set[str]):
        """Used to receive Messages with certain tags from the environment."""
        self.state.addresses = addresses
        if self.state.env:
            self.state.env.set_addresses(self, self.state.addresses)

    def get_cwd(self) -> str:
        """Current working directory.

        Capability surface for tools; the cwd fallback logic lives on the
        :class:`RoleStateController` (state ownership stays out of tools).
        """
        return self._state_ctl.get_cwd()

    def set_cwd(self, path: str) -> None:
        """Set the stable working directory (framework API for an explicit switch).

        Not called by the Bash tool — a `cd` inside a command does not drift
        the cwd (Codex-aligned: cwd is stable data). The capability for a
        deliberate directory-change entry point. Delegates to the state
        controller.
        """
        self._state_ctl.set_cwd(path)

    def current_turn_index(self) -> int:
        """The current turn (prompt) index used to attribute change hunks.

        Capability surface for the hunk-attribution path (executor settle and
        the read-before-write guard): each captured hunk is stamped with this
        value so the review layer can group pending changes by turn. Delegates
        to the state controller.
        """
        return self._state_ctl.current_turn_index()

    def _advance_turn(self) -> int:
        """Advance the turn index by one (injected into the react loop).

        Called once per think round so hunks captured during a turn carry a
        stable, monotonic index. Framework-internal (not a tool capability).
        """
        return self._state_ctl.advance_turn()

    def get_default_model(self) -> Optional[str]:
        """Name of the default (main think-loop) model, or None if unconfigured.

        Capability surface for media tools (Read / WebBrowser screenshot): the
        media a tool attaches rides the MAIN model's request, so a tool checks
        ``supports_vision`` / ``supports_pdf_input`` against this name to refuse
        up-front (ToolNotConfiguredError) rather than attach media the model
        silently cannot read.
        """
        return getattr(self.config.models.default, "model", None)

    def record_file_read(self, path: str, mtime_ns: int) -> None:
        """Record that a file was read.

        The Read tool calls this after a successful read so the Write/Edit tools
        can later enforce read-before-overwrite and detect external
        modifications — without ever touching RoleState directly.
        """
        self._state_ctl.record_file_read(path, mtime_ns)

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        """Return the mtime_ns recorded when `path` was last read, else None.

        Counterpart to record_file_read(). The Write/Edit tools compare the
        returned value against the file's current mtime to decide whether the
        model has seen the latest content before overwriting it.
        """
        return self._state_ctl.get_file_read_mtime(path)

    def record_file_glimpsed(self, path: str) -> None:
        """Record that a file surfaced in a search result (Grep/Glob), un-read.

        The Grep/Glob tools call this for each file they matched so the code map
        can surface those files' structure (defines + intent) as a "which of
        these should I open" hint — without the file's body ever entering
        context. Kept separate from record_file_read(): a glimpse carries no body
        and must not trip the read-before-write guard.
        """
        self._state_ctl.record_file_glimpsed(path)

    def is_resource_visible(self, path: str) -> bool:
        """Is the most-recent tool result read from `path` still present in context?

        Delegates to :class:`~mote.context.visibility.ContextVisibility`. A
        deduplicating read tool consults this before returning a "you already
        read this" stub: if the earlier result has been folded/erased by
        compaction the stub would strand the model with no content, so the tool
        must re-read instead. Read-only; never mutates history.
        """
        return self._components.context_visibility.is_resource_visible(path)

    def get_tool_session(self, key: str) -> Any:
        """Return a stateful tool's live per-Role session (keyed by tool name).

        Stateful tools (terminal shell, Python kernel) store their live session
        on RoleState through this capability + :meth:`set_tool_session` instead
        of a process-global singleton, so the session is owned by this Role,
        isolated per session, and torn down with it. Returns None when no
        session is live yet (the tool creates one on first use).
        """
        return self._state_ctl.get_tool_session(key)

    def set_tool_session(self, key: str, value: Any) -> None:
        """Store/clear a stateful tool's live session (a None value clears it).

        Counterpart to :meth:`get_tool_session`. The terminal/kernel tools call
        this to register a newly started session and to drop it on close /
        teardown, without ever touching RoleState directly.
        """
        self._state_ctl.set_tool_session(key, value)

    def get_skill_pool(self) -> Optional[SkillPool]:
        """Return the live SkillPool, or None when skills are disabled.

        Capability surface for the ``Skill`` bridge tool; delegates to
        :class:`RoleCapabilities`.
        """
        return self._capabilities.get_skill_pool()

    async def run_skill_fork(self, **kwargs) -> str:
        """Run a ``context: fork`` skill inside a fresh, isolated child Role.

        Capability surface for the ``Skill`` bridge tool; delegates to
        :class:`RoleCapabilities` (which owns the child lifecycle, including
        cleanup).
        """
        return await self._capabilities.run_skill_fork(**kwargs)

    # =========================================================================
    # Narrow capabilities exposed to tools (injected via BaseTool.requires).
    # Tools call these instead of receiving RoleState/memory/env directly, so
    # role behavior stays in the Role and tools stay thin triggers.
    # =========================================================================

    def tool_capabilities(self) -> CapabilityMap:
        """The explicit allowlist of capabilities a tool may receive via bind().

        BaseTool.bind() resolves each name in a tool's `requires` against this
        map — and ONLY this map — so a tool can never reach RoleState, memory,
        or any Role attribute that is not deliberately published here. Adding a
        capability is an explicit decision; `getattr(role, ...)` is never used.
        """
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "get_default_model": self.get_default_model,
            "deactivate": self.deactivate,
            "ask_user": self.ask_user,
            "ask_user_question": self.ask_user_question,
            "get_bg_pool": self.get_bg_pool,
            "request_approval": self.request_approval,
            "reply_to_user": self.reply_to_user,
            "end_session": self.end_session,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "record_file_glimpsed": self.record_file_glimpsed,
            "is_resource_visible": self.is_resource_visible,
            "record_file_snapshot": self._capabilities.record_file_snapshot,
            "record_file_baseline": self._capabilities.record_file_baseline,
            "attribute_external_change": self._capabilities.attribute_external_change,
            "record_terminal_state": self._capabilities.record_terminal_state,
            "take_pending_terminal_restore": self._state_ctl.take_pending_terminal_restore,
            "record_kernel_state": self._capabilities.record_kernel_state,
            "take_pending_kernel_restore": self._state_ctl.take_pending_kernel_restore,
            "record_browser_state": self._capabilities.record_browser_state,
            "take_pending_browser_restore": self._state_ctl.take_pending_browser_restore,
            "get_browser_headless": self.get_browser_headless,
            "get_browser_stealth": self.get_browser_stealth,
            "get_browser_locale": self.get_browser_locale,
            "get_browser_proxy": self.get_browser_proxy,
            "get_browser_profile": self.get_browser_profile,
            "load_browser_profile": self.load_browser_profile,
            "save_browser_profile": self.save_browser_profile,
            "get_browser_client_certs": self.get_browser_client_certs,
            "get_browser_cdp_endpoint": self.get_browser_cdp_endpoint,
            "get_secret": self.get_secret,
            "get_tool_session": self.get_tool_session,
            "set_tool_session": self.set_tool_session,
            "wait_interruptible": self.wait_interruptible,
            "get_skill_pool": self.get_skill_pool,
            "run_skill_fork": self.run_skill_fork,
            "register_resource": self._capabilities.register_resource,
            "register_task_result": self._capabilities.register_task_result,
            "retire_task_result": self._capabilities.retire_task_result,
            "get_sandbox_runtime": self.get_sandbox_runtime,
            "get_device_config": self.get_device_config,
            "dispatch_tool": self.dispatch_tool,
            "list_tool_names": self.list_tool_names,
            "list_graph_tool_names": self.list_graph_tool_names,
            "list_graph_excluded_tool_names": self.list_graph_excluded_tool_names,
            "commit_graph_output": self._components.graph_output_service.finalize,
            "resume_graph_output": self._components.graph_output_service.resume,
            "has_graph_output_restore": self._state_ctl.has_pending_graph_output_restore,
            "graph_run_lease": self.graph_run_lease,
            "list_deferred_tools": self.list_deferred_tools,
            "reveal_tools": self.reveal_tools,
            "describe_deferred_tools": self.describe_deferred_tools,
            "web_search": self.web_search,
            "describe_image": self.describe_image,
        }

    def deactivate(self) -> None:
        """Stop the react loop after the current step."""
        self._state_ctl.deactivate()

    def _is_active(self) -> bool:
        """Read the shared active signal (consumed by the loop's think step)."""
        return self._state_ctl.is_active()

    def _set_active(self, value: bool) -> None:
        """Write the shared active signal (used by the loop each iteration).

        `active` lives on RoleState — not inside the loop — because it doubles
        as a tool→loop kill switch: the End tool and ask_user's "stop" call
        deactivate(), which must still break a loop that is mid-run.
        """
        self._state_ctl.set_active(value)

    def get_bg_pool(self) -> BackgroundTaskPool:
        """Return the background task pool (capability surface; delegates)."""
        return self._capabilities.get_bg_pool()

    def get_sandbox_runtime(self) -> Optional[SandboxRuntime]:
        """Return the OS-level sandbox runtime, or ``None`` when not configured.

        Capability surface for the command-execution tools (Bash / terminal /
        python). ``None`` when ``permissions.runtime`` is absent/disabled, in
        which case those tools run un-sandboxed (the historical behavior).
        """
        return self._components.sandbox_runtime

    def get_device_config(self) -> DeviceConfig:
        """Return the DeviceUse tool's device-backend config (``config.tools.device``).

        Capability surface for the DeviceUse tool: selects the device backend
        (``auto``/``android``/``none``) and how to reach an adb device
        (``adb_path`` + ``serial``), without the executor layer reaching into the
        config. Defaults to ``auto`` (Android when adb is reachable, else null).
        """
        return self.config.tools.device

    def get_browser_headless(self) -> bool:
        """Return the role's ``browser_headless`` flag (True => run headless).

        Capability surface for the WebBrowser tool: lets the tool launch headed
        (a visible window) when the role opts in, without the executor layer
        reaching into the role schema. Defaults to True (headless).
        """
        return self.role_schema.browser_headless

    def get_browser_stealth(self) -> bool:
        """Return the role's ``browser_stealth`` flag (True => anti-detection).

        Capability surface for the WebBrowser tool: lets the tool apply the
        opt-in stealth measures (realistic UA, ``navigator.webdriver`` hiding,
        launch flags) when the role opts in, without the executor layer reaching
        into the role schema. Defaults to False (no anti-detection).
        """
        return self.role_schema.browser_stealth

    def get_browser_locale(self) -> str:
        """Return the browser locale/region bundle key ("auto"/"en"/"zh").

        Capability surface for the WebBrowser tool: selects which coherent locale
        bundle the stealth fingerprint uses (only when ``browser_stealth`` is on).
        Resolution: an explicit per-role ``role_schema.browser_locale`` (anything
        other than "auto") wins, else it falls back to the global
        ``config.tools.browser_locale`` from config.yaml. When both are "auto"
        the engine infers zh vs en from the host env.
        """
        if self.role_schema.browser_locale != "auto":
            return self.role_schema.browser_locale
        configured = self.config.tools.browser_locale or ""
        if configured:
            return configured
        return "auto"

    def get_browser_proxy(self) -> str:
        """Return the browser's proxy URL (empty = direct connection).

        Capability surface for the WebBrowser tool: a single proxy URL giving the
        session one exit IP (parsed engine-side into Playwright's launch proxy
        dict). Resolution order (first non-empty wins):
          1. per-role ``role_schema.browser_proxy``;
          2. global ``config.tools.proxy`` from config.yaml (documented there as
             the proxy "for tools such as browsers");
          3. the ambient proxy env vars (``HTTPS_PROXY`` / ``HTTP_PROXY`` /
             ``ALL_PROXY``, case-insensitive) — so a shell that already exports a
             proxy routes the browser through it with no config. Playwright's
             Chromium does not read these itself, so we forward them explicitly.
        Defaults to "".
        """
        if self.role_schema.browser_proxy:
            return self.role_schema.browser_proxy
        configured = self.config.tools.proxy or ""
        if configured:
            return configured
        for var in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            value = os.environ.get(var, "")
            if value:
                return value
        return ""

    def get_browser_profile(self) -> str:
        """Return the role's durable browser-login profile name (empty = ephemeral).

        Capability surface for the WebBrowser tool: when non-empty, the tool
        seeds/persists the logged-in session from an encrypted profile under
        ``~/.mote/browser_profiles/`` instead of leaving login ephemeral.
        """
        return self.role_schema.browser_profile

    def load_browser_profile(self, name: str) -> Optional[dict]:
        """Return the decrypted ``storage_state`` saved under *name* (or None).

        Capability surface for the WebBrowser tool; delegates to the encrypted
        :class:`BrowserProfileStore`. Best-effort (None on any miss/failure).
        """
        return self.browser_profile_store.load(name)

    def save_browser_profile(self, name: str, storage_state: Optional[dict]) -> None:
        """Persist *storage_state* under *name* in the encrypted profile store.

        Capability surface for the WebBrowser tool; delegates to the encrypted
        :class:`BrowserProfileStore`. Best-effort (never raises).
        """
        self.browser_profile_store.save(name, storage_state)

    def get_browser_client_certs(self) -> list[dict]:
        """Return the role's client TLS certs as Playwright-shaped dicts.

        Capability surface for the WebBrowser tool (mutual-TLS logins). Maps each
        ``role_schema.browser_client_certs`` entry to a Playwright
        ``client_certificates`` dict: ``origin`` plus whichever of ``certPath`` /
        ``keyPath`` / ``pfxPath`` / ``passphrase`` are set (omitting empties). The
        ``passphrase`` is returned VERBATIM — it may be a secret placeholder that
        the tool expands from the vault at launch time — so no plaintext leaves
        the vault here. Empty list (default) means no mTLS.
        """
        out: list[dict] = []
        for cert in self.role_schema.browser_client_certs:
            entry: dict = {"origin": cert.origin}
            if cert.cert_path:
                entry["certPath"] = cert.cert_path
            if cert.key_path:
                entry["keyPath"] = cert.key_path
            if cert.pfx_path:
                entry["pfxPath"] = cert.pfx_path
            if cert.passphrase:
                entry["passphrase"] = cert.passphrase
            out.append(entry)
        return out

    def get_browser_cdp_endpoint(self) -> str:
        """Return the CDP endpoint to attach to (empty = launch our own browser).

        Capability surface for the WebBrowser tool: when non-empty the tool
        attaches to an already-running Chrome over the DevTools Protocol instead
        of launching a private browser (reusing the human's real logins).
        """
        return self.role_schema.browser_cdp_endpoint

    def get_secret(self, key: str) -> Optional[str]:
        """Resolve a named secret to its plaintext value, or ``None`` if unknown.

        Capability surface for autonomous login-fill (WebBrowser): a tool
        references a credential **by key** (e.g. a ``<agent-vault:KEY>`` /
        ``<totp:KEY>`` placeholder the model wrote) and this resolves it against
        the encrypted vault — the model never authors or sees the value. Returns
        ``None`` when the key is unknown or secrets are disabled (no store),
        which the reference expander treats as fail-closed.
        """
        store = self._components.secret_store
        return store.get(key) if store is not None else None

    def list_secret_names(self) -> dict[str, str]:
        """Return the ``{name: placeholder}`` map of configured secret NAMES.

        The discovery complement of :meth:`get_secret`: the credential-index
        turn-context source reads this to show the model WHICH named secrets it
        can reference (and the exact placeholder to write), never any value.
        Reads the same vault; returns ``{}`` when secrets are disabled (no
        store). Wired into the context source by a lambda in ``role_components``
        (like the deferred-tool menu) — not published as a tool capability,
        because no tool consumes it.
        """
        store = self._components.secret_store
        return store.labels() if store is not None else {}

    async def ask_user(self, question: str) -> str:
        """Ask the user a question and return their response.

        Only valid inside a MoteEnv. A trailing 'stop' deactivates the role.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.ask_user(question)

    async def ask_user_question(self, questions: list[AskUserQuestionItem]) -> AskUserQuestionAnswers:
        """Ask the user structured multiple-choice questions; return structured answers.

        Capability surface behind the ``AskUserQuestion`` tool; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.ask_user_question(questions)

    async def request_approval(self, request: "ApprovalRequest") -> "ApprovalChoice":
        """Ask the human to approve a gated tool call; return their decision.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.request_approval(request)

    async def reply_to_user(self, content: str) -> str:
        """Reply to the user with the provided content.

        Only valid inside a MoteEnv. Capability surface; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.reply_to_user(content)

    async def wait_interruptible(self, duration: "float | None" = None) -> float:
        """Block until an event wakes the agent, optionally bounded by *duration*.

        Capability surface for the Sleep tool; delegates to
        :class:`RoleCapabilities` (which owns the wait coordination). A positive
        *duration* opens a durable timer whose deadline survives a crash-resume.
        """
        return await self._capabilities.wait_interruptible(duration)

    async def dispatch_tool(self, name: str, kwargs: "dict | None" = None) -> "ToolResult":
        """Dispatch a nested tool call through the executor chokepoint.

        Capability surface for the ``run_graph`` orchestrator: every graph-node
        tool call is routed back through ``ToolExecutor.run_command``, so the same
        permission gate, hooks, and observability that guard a direct tool call
        apply identically to graph-driven calls (re-entrant safe). Returns the
        tool's ``ToolResult`` — a denied/failed call is ``success=False``, not raised.

        Deliberately passes NO ``result_id``, so graph-internal calls are not
        individually ledgered by the effect-ledger. Crash recovery for a graph is
        two-level: (1) the foreground ``run_graph`` runs inside ONE top-level
        ``run_command`` whose own EXTERNAL ledger entry (RunGraph resolves to
        EXTERNAL) is the crash-recovery unit — a resume reconciles that single
        call; (2) in-flight pause/resume re-runs only not-yet-completed NODES.
        Ledgering each node call would be unreconcilable: a graph-internal call
        never surfaces as a ``tool_calls`` entry in the durable history, so the
        resume reconciler could never pair (and reap) it — its ``started`` record
        would leak forever. So node-level idempotency stays a graph concern
        (node-replay), and the ledger guards the graph as a whole.
        """
        return await self.executor.run_command(name, kwargs or {})

    @asynccontextmanager
    async def graph_run_lease(self, run_id: str):
        """Own a graph from recovery lookup through its terminal commit."""
        await self._components.begin_graph_lease(run_id)
        try:
            yield
        finally:
            await self._components.end_graph_lease(run_id)

    def list_tool_names(self) -> list[str]:
        """Live tool names (primary + aliases), for ``run_graph`` to validate the
        tool references in a graph spec. Capability surface over the executor."""
        return self.executor.tool_names()

    def list_graph_tool_names(self) -> list[str]:
        """Names of tools that are themselves graph orchestrators, for ``run_graph``
        to refuse nesting a graph inside a graph. Capability surface over the
        executor's ``is_graph_tool`` marker set."""
        return sorted(self.executor.graph_tool_names())

    def list_graph_excluded_tool_names(self) -> list[str]:
        """Names of tools that must not appear as a node inside a graph, for
        ``run_graph`` to refuse referencing them (e.g. Sleep, which blocks on an
        external wake event a foreground graph never delivers). Capability surface
        over the executor's ``graph_excluded`` marker set."""
        return sorted(self.executor.graph_excluded_tool_names())

    def list_deferred_tools(self) -> dict[str, str]:
        """The deferred-tool MATCH corpus → ``{name: summary + recall keywords}``.

        Capability surface for the ``SearchTools`` meta-tool: the full set of
        deferred (not-yet-revealed) tools it searches over, each entry enriched
        with the tool's recall keywords so a synonym the one-line summary omits
        still resolves. This is the SEARCH layer — distinct from the DISPLAY menu
        the model reads (``deferred_tool_index``, pure summaries). Delegates to
        the executor's tool catalog (the single deferral seam)."""
        return self.executor.deferred_search_index()

    def reveal_tools(self, names: list[str]) -> list[str]:
        """Reveal deferred tools by name so their full schema is sent next turn.

        Capability surface for the ``SearchTools`` meta-tool. Intersects *names*
        with the executor's deferred set (so only real deferred tool names are
        accepted — a bogus/non-deferred name is ignored), records the accepted
        names on ``RoleState`` (durable across resume) and returns them. The next
        ``prepare()`` includes their schema on the active channel."""
        deferred = set(self.executor.deferred_tool_index().keys())
        accepted = [n for n in names if n in deferred]
        self._state_ctl.reveal_tools(accepted)
        return accepted

    def describe_deferred_tools(self, names: list[str]) -> dict[str, str]:
        """Full descriptions of the named deferred tools → ``{name: description}``.

        Capability surface for the ``SearchTools`` meta-tool: on reveal it reads
        the newly-revealed tools' full prose (stripped off the split ``tools=``
        wire) so it can persist that description into the conversation +
        ResourceRegistry. Delegates to the executor's tool catalog."""
        return self.executor.describe_deferred_tools(names)

    async def web_search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        max_uses: int = 8,
    ) -> "list[WebSearchHit]":
        """Run a provider-native server-side web search and return the hits.

        Capability surface for the ``WebSearch`` tool. Routes an ISOLATED
        secondary LLM call via the ``web_search`` task (a small/fast model — see
        ``ModelsConfig`` — that itself supports server-side web search) so the
        tool never touches the router/Context directly, mirroring every other
        capability. The routed provider issues the actual search+crawl through
        its server-side tool (Anthropic ``web_search_20250305`` / OpenAI
        Responses ``web_search``) and returns structured hits.

        Raises:
            NotImplementedError: The routed provider/model has no server-side web
                search — the tool degrades to a "search unavailable" notice.
        """
        llm = self.router.route_for_task("web_search")
        return await llm.aweb_search(
            query,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            max_uses=max_uses,
        )

    async def describe_image(self, image_b64: str, *, prompt: str = "") -> str:
        """Read an image as text via an ISOLATED vision-model call.

        Capability surface for ``WebBrowser``'s ``read_image`` action. Routes a
        secondary LLM call via the ``image_description`` task (a multimodal
        small/fast model — see ``ModelsConfig``) so the caller never touches the
        router/Context directly, mirroring every other capability. It reads an
        on-page image as text: normally an image reaches the model directly as
        media (Read → ToolMedia), but the browser has no such wire for an
        in-page ``<img>`` — this routes it to a vision model instead, whose
        textual reading then re-enters the conversation as ordinary text.

        Raises:
            NotImplementedError: The routed model is not vision-capable — the
                tool degrades to an "image understanding unavailable" notice.
        """
        llm = self.router.route_for_task("image_description")
        return await llm.adescribe_image(image_b64, prompt=prompt)

    async def end_session(self) -> str:
        """End the current session and produce a summary if configured.

        Capability surface for the End tool; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.end_session()

    def get_memories(self, k=0) -> list[Message]:
        return self.context_manager.get(k=k)

    def publish_message(self, msg):
        """If the role belongs to env, then the role's messages will be broadcast to env"""
        if not msg:
            return
        if MESSAGE_ROUTE_TO_SELF in msg.send_to:
            msg.send_to.add(any_to_str(self))
            msg.send_to.remove(MESSAGE_ROUTE_TO_SELF)
        if not msg.sent_from or msg.sent_from == MESSAGE_ROUTE_TO_SELF:
            msg.sent_from = any_to_str(self)
        if all(to in {any_to_str(self), self.role_schema.name} for to in msg.send_to):
            self.put_message(msg)
            return
        if not self.state.env:
            return
        if isinstance(msg, AIMessage) and not msg.agent:
            msg.with_agent(self.role_schema.display_name)
        self.state.env.publish_message(msg)

    def put_message(self, message):
        """Place the message into the Role object's private message buffer."""
        self._state_ctl.put_message(message)

    def _coerce_to_message(self, with_message) -> Message:
        """Normalize the run() input (str / list / Message) into one Message.

        Stamps the default USER_REQUIREMENT cause and routes the message to this
        role so the loop observes it. Kept out of run() so the dispatch table
        stays readable.
        """
        if isinstance(with_message, Message):
            msg = with_message
        elif isinstance(with_message, list):
            msg = Message(content="\n".join(with_message))
        else:
            msg = Message(content=with_message)
        if not msg.cause_by:
            msg.cause_by = CauseBy.USER_REQUIREMENT
        msg.send_to.add(self.role_schema.name)
        return msg

    async def _emit_session_start(self) -> None:
        """Emit ``SessionStartEvent`` exactly once across this Role's run() calls.

        The HookSubscriber fires the SessionStart hook; the recorder's meta line
        is written here at the explicit startup boundary. Also starts the opt-in
        external-file watcher (the property is None when disabled / no hook
        layer); its polling loop is stopped in :meth:`cleanup`.
        """
        if self._session_started:
            return
        self._session_started = True

        # Establish the append-only log before SessionStart observers can append
        # anything. Component resolution itself is intentionally I/O-free.
        self.session_log.create(
            SessionMetaEvent(
                session_id=self.state.session_id,
                parent_session_id=self.state.parent_session_id,
                working_dir=self.state.working_dir,
                original_working_dir=self.state.original_working_dir,
                project_root=self.state.project_root,
                model=getattr(self.config.models.default, "model", None),
                role_class=f"{type(self).__module__}.{type(self).__qualname__}",
            )
        )

        # Open this session's own log file (logs/{session_id}.txt), named to
        # match its workspace session folder. run() has bound session_id as the
        # trace_id, so the sink's filter routes this session's lines here.
        bind_session_logfile(self.session_id)

        await self.event_bus.emit(
            SessionStartEvent(
                session_id=self.state.session_id,
                parent_session_id=self.state.parent_session_id,
                working_dir=self.state.working_dir,
                original_working_dir=self.state.original_working_dir,
                project_root=self.state.project_root,
                model=getattr(self.config.models.default, "model", None),
                role_class=f"{type(self).__module__}.{type(self).__qualname__}",
                source="startup",
            )
        )

        watcher = self.file_watch_service
        if watcher is not None and not watcher.watcher.is_running():
            await watcher.start_async()

        # Kick off the whole-repo code-index cold scan off the event loop (Layer
        # C). No-op when the index layer is off; best-effort inside.
        await self._components.kickoff_repo_scan()

        # Reclaim stale workspace artifacts off the event loop (throttled ~daily,
        # excludes this session). No-op when disabled; best-effort inside.
        await self._components.kickoff_workspace_cleanup()

    @role_raise_decorator
    async def run(self, with_message=None) -> RunResult[OutputT] | None:
        """Run one request and return its typed, durably committed output."""
        # Bind the session_id as the trace_id so every log line emitted during
        # this run (across the loop, think engine, executor, etc.) is correlated.
        # Bind the event bus to the async context so deep call sites (the LLM
        # client streaming tokens, a tool capturing a snapshot) can emit onto the
        # same spine without threading the bus through every signature.
        with bind_trace(self.session_id), set_bus(self.event_bus):
            async with span(f"role.run:{self.name}"):
                await self._ensure_ready()
                await self._emit_session_start()

                if with_message:
                    msg = self._coerce_to_message(with_message)

                    # UserPromptSubmit event: a subscriber (hook) may inject extra
                    # context (prepended to the prompt) or veto the turn (stop ->
                    # deactivate before loop). Emitting always; the folded outcome is
                    # EMPTY when no hook layer is wired.
                    outcome = await self.event_bus.emit(UserPromptSubmitEvent(prompt=msg.content))
                    # ``None`` when no hook layer is wired (nothing to inject/veto).
                    if outcome is not None:
                        # A rewrite (secret-upload vaulting) replaces the prompt
                        # *before* context-prepend + storage, so the raw value
                        # never reaches the model, history, or logs.
                        if outcome.updated_prompt is not None:
                            msg.content = outcome.updated_prompt
                        if outcome.additional_context:
                            injected = "\n".join(outcome.additional_context)
                            msg.content = f"{injected}\n{msg.content}" if msg.content else injected
                        if outcome.stop:
                            self.deactivate()

                    # LSP diagnostics now flow through the per-turn ephemeral-context
                    # bus (turn_context layer): drained every think() cycle into the
                    # user prompt's <system-reminder> (never stored in history),
                    # alongside git status / token pressure / background-task feeds.
                    self.put_message(msg)

                # Auto-continue budget (opt-in, default 0): a TurnEnd control
                # subscriber may block the "stop" to force another turn
                # (Stop-hook semantics). The budget bounds it so a misbehaving
                # policy can never loop forever; with the default 0 (and no such
                # subscriber wired) the loop runs exactly once.
                auto_continue_budget = self.role_schema.max_auto_continue
                loop_result = None
                while True:
                    await self._components.begin_output_lease()
                    try:
                        loop = self._components.make_loop()
                        try:
                            loop_result = await loop.run()
                        finally:
                            # Always propagate for recovery (role_raise_decorator reads it).
                            self.state.latest_observed_msg = loop.latest_observed_msg
                            # TurnEnd event: the recorder marks the turn boundary in the
                            # durable log (working_dir may have moved via `cd`, so capture
                            # the live value at turn end) and the HookSubscriber fires the
                            # Stop hook. Guarded on the slot so a failure before the bus was
                            # built never triggers lazy construction in teardown.
                            turn_outcome = await self._emit_turn_end()
                    finally:
                        await self._components.end_output_lease()
                    if loop_result is not None and loop_result.committed_output is not None:
                        break
                    if not self._should_auto_continue(turn_outcome, auto_continue_budget):
                        break
                    auto_continue_budget -= 1
                if loop_result is None:
                    return None
                rsp = loop_result.presentation

                # Post-loop finalization (was Role.react): clear the active signal
                # and tag the response with this Role's display name.
                self._state_ctl.deactivate()
                if isinstance(rsp, AIMessage):
                    rsp.with_agent(self.role_schema.display_name)
                committed = loop_result.committed_output
                if committed is not None:
                    publication_id = f"output:{self.session_id}:{committed.run_id}"
                    await self.event_bus.emit(
                        OutputPublicationQueuedEvent(
                            publication_id=publication_id,
                            candidate_id=committed.candidate_id,
                            contract_id=committed.contract_id,
                            run_id=committed.run_id,
                            run_kind=committed.run_kind.value,
                        )
                    )
                    await get_disk_writer().drain()
                    rsp.metadata["output_publication_id"] = publication_id
                    self.publish_message(rsp)
                    await self.event_bus.emit(
                        OutputPublishedEvent(
                            candidate_id=committed.candidate_id,
                            contract_id=committed.contract_id,
                            publication_id=publication_id,
                            run_id=committed.run_id,
                            run_kind=committed.run_kind.value,
                        )
                    )
                    await get_disk_writer().drain()
                    return RunResult(
                        output=committed.value,
                        output_record=committed,
                        transcript=TranscriptRef(
                            session_id=self.session_id,
                            terminal_message_id=str(rsp.id),
                        ),
                        run_id=committed.run_id,
                    )
                self.publish_message(rsp)
                return None

    @staticmethod
    def list_sessions(base_dir: str | None = None, *, cwd: str | None = None) -> list:
        """List resumable sessions (newest first); see ``session.list_sessions``.

        A thin, discoverable entry point onto the lite directory scan. ``cwd``
        filters to sessions started under that working dir / project root.
        """

        return _list(base_dir, cwd=cwd)

    def resume_session(self) -> bool:
        """Rebuild this role's stored history from its durable rollout log.

        Thin delegator onto :class:`RoleSessionManager` (which owns the replay +
        registry/restore re-seeding). Returns False when no log exists.
        """
        return self._session_manager.resume()

    def fork_session(self) -> "Role":
        """Branch a sibling role off this session at its current history.

        Thin delegator onto :class:`RoleSessionManager`; returns a fresh role of
        the same class resumed onto the inherited history, independent afterwards.
        """
        return self._session_manager.fork()

    def _should_auto_continue(self, turn_outcome: Optional[Any], budget: int) -> bool:
        """Decide whether the run loop should force another turn.

        The auto-continue seam (framework only — no built-in policy subscriber):
        a TurnEnd control subscriber signals "don't stop, keep going" by folding
        a :class:`TurnOutcome` with ``block=True`` (the stop is *blocked*, the
        inverse of a UserPromptSubmit ``stop`` that aborts). We honor it only
        while the budget allows, and enqueue any context the policy supplied as
        the next turn's prompt so the model knows *why* it was asked to continue.
        With the default budget 0 (and no such subscriber) this is always
        ``False`` — the loop runs once, unchanged.
        """
        if budget <= 0 or turn_outcome is None or not turn_outcome.block:
            return False
        injected = "\n".join(turn_outcome.additional_context) or turn_outcome.system_message
        if injected:
            self.put_message(self._coerce_to_message(injected))
        return True

    async def _emit_turn_end(self) -> Optional[Any]:
        """Emit ``TurnEndEvent`` delimiting one completed turn; return its outcome.

        Carries the per-turn runtime snapshot (working_dir may have moved via
        `cd`; token_state is optional metadata). The recorder subscriber maps it
        to a ``turn_context`` log record and the hook subscriber fires the Stop
        hook. Best-effort: skipped when the bus was never built (e.g. the run
        failed before _ensure_ready) and a failure never breaks the turn.

        Returns the folded :class:`TurnOutcome` so the run loop can honor an
        auto-continue policy: a control subscriber may ``block`` the turn end
        (block the stop) to force another turn (Stop-hook semantics). Returns
        ``None`` when there is no bus, the emit failed, or nothing maps TurnEnd
        (never continue).
        """
        bus = self._components.peek_event_bus()
        if bus is None:
            return None
        try:
            token_state = None
            try:
                token_state = asdict(self.context_manager.token_state())
            except Exception:  # noqa: BLE001 — token math is optional metadata
                token_state = None
            return await bus.emit(
                TurnEndEvent(
                    turn_id=uuid4().hex,
                    working_dir=self.state.working_dir,
                    model=getattr(self.config.models.default, "model", None),
                    token_state=token_state,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"session: failed to emit turn end: {exc}")
            return None

    async def cleanup(self) -> None:
        """Tear down session-scoped subsystems (best-effort, idempotent).

        Stops the file-watch polling loop (and detaches it from the event bus),
        shuts the LSP language servers down, then delegates to
        :meth:`ToolExecutor.cleanup` (which closes the terminal/kernel). Safe to
        call when those subsystems were never built — each guard short-circuits.
        """
        file_watch_service = self._components.peek_file_watch_service()
        if file_watch_service is not None:
            try:
                await file_watch_service.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.warning(f"Role: file_watch_service.stop() failed: {exc}")
        lsp_service = self._components.peek_lsp_service()
        if lsp_service is not None:
            try:
                await lsp_service.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.warning(f"Role: lsp_service.shutdown() failed: {exc}")
        executor = self._components.peek_executor()
        if executor is not None:
            await executor.cleanup()
        sandbox_runtime = self._components.peek_sandbox_runtime()
        if sandbox_runtime is not None:
            try:
                await sandbox_runtime.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.warning(f"Role: sandbox_runtime.shutdown() failed: {exc}")
        repo_index = self._components.peek_repo_index()
        if repo_index is not None:
            try:
                repo_index.close()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.warning(f"Role: repo_index.close() failed: {exc}")
        # Close this session's per-session log sink (frees the file handle; a
        # long-lived CLI process opens many sessions). Idempotent / no-op when
        # file logging was disabled or the session never emitted its start.
        unbind_session_logfile(self.session_id)

    # =========================================================================
    # Readiness
    # =========================================================================

    async def _ensure_ready(self):
        """Lazy init for expensive/fallible subsystems."""
        # Materialize the ContextManager (stored-history store + compaction
        # orchestrator), backed by RoleState.context so it survives recovery.
        _ = self.context_manager

        # Wire the event spine (subscribe the roster). The ``event_bus`` getter is
        # a pure leaf — it never wires itself — so this explicit step is the sole
        # trigger, guaranteeing the spine is wired before the first ``emit`` below
        # (``set_bus`` in run() bound the same leaf, mutated in place here).
        self._components._wire_spine()

        # Wire runtime edges between built collaborators (the router's COMPRESS
        # reducer ← ContextManager). Split out of the getters so no component
        # read mutates a sibling as a hidden side-effect.
        self._components._wire_collaborators()

        self.skill_manager.ensure_ready()
        await self.executor.init_mcp(self.role_schema.mcps, enabled=self.config.mcp.enabled)
