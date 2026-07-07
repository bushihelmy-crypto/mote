#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/5/11 14:42
@Author  : alexanderwu
@File    : role.py
@Merged  : role.py + role_zero.py — single unified Role class.
@Refactored: Remove Pydantic inheritance. Role is now a plain ABC class.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Optional, Set
from uuid import uuid4

from metagpt.common.base import BaseRole
from metagpt.common.const import MESSAGE_ROUTE_TO_SELF
from metagpt.common.events import (
    SessionStartEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
    set_bus,
    span,
)
from metagpt.common.exception import RoleContextNotSetError
from metagpt.common.logs import bind_trace, log_class, logger
from metagpt.common.schema import AIMessage, CauseBy, Message, UserMessage
from metagpt.common.utils.common import any_to_str, role_raise_decorator
from metagpt.context import ContextManager
from metagpt.context.skills.skill_manager import SkillManager
from metagpt.executor.tasks import BackgroundTaskPool
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.loop import BaseLoop, ReActLoop
from metagpt.parser import CommandChannel
from metagpt.roles.capabilities import RoleCapabilities
from metagpt.roles.context_provider import ContextProvider
from metagpt.roles.role_components import RoleComponents
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState, RoleStateController
from metagpt.router.router import LLMRouter
from metagpt.session import SessionLog, fork
from metagpt.session import list_sessions as _list
from metagpt.session import replay
from metagpt.think.think_engine import ThinkEngine

if TYPE_CHECKING:
    from metagpt.session import (
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
        "record_file_snapshot",
        "put_message",
        "publish_message",
        "tool_capabilities",
        "deactivate",
        "get_memories",
        "set_env",
        "set_addresses",
    },
)
class Role(BaseRole):
    """Unified Role/Agent — pure orchestration via composition.

    Composes:
      - role_schema: RoleSchema (static config, deploy-time)
      - state: RoleState (runtime snapshot, serializable for checkpoint/recovery)
      - Lazy-init components: ThinkEngine, ToolExecutor, SkillManager, etc.

    No longer inherits from Pydantic BaseModel. Construction is explicit via __init__.
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
        # Behaviour over the (pure-DTO) state lives in a controller; the Role's
        # state methods below are thin delegators onto it (the capability surface).
        self._state_ctl = RoleStateController(self.state)
        # Subsystem-backed tool capabilities (human I/O, sleep, end-of-session
        # summary, skill forks, the task/skill pools) live in a holder for the
        # same reason; the Role's capability methods below delegate onto it.
        self._capabilities = RoleCapabilities(self)

        # External dependencies (injected)
        self._context = context
        self._config = config

        # Lazy assembly + ownership of all subsystems (router, executor, context
        # manager, event bus, session log, hook/LSP/file-watch services, the
        # per-turn context bus, …). The Role keeps a thin property surface that
        # delegates onto this holder; the wiring logic lives there.
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
    def terminal_state_recorder(self) -> "TerminalStateRecorder":
        return self._components.terminal_state_recorder

    @property
    def kernel_state_recorder(self) -> "KernelStateRecorder":
        return self._components.kernel_state_recorder

    @property
    def browser_state_recorder(self) -> "BrowserStateRecorder":
        return self._components.browser_state_recorder

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

    @property
    def compaction_notice(self):
        return self._components.compaction_notice

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

    @property
    def think_engine(self) -> ThinkEngine:
        return self._components.think_engine

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
        """Current working directory, aligned with Claude Code's getCwd().

        Capability surface for tools; the cwd fallback logic lives on the
        :class:`RoleStateController` (state ownership stays out of tools).
        """
        return self._state_ctl.get_cwd()

    def set_cwd(self, path: str) -> None:
        """Persist the live working directory, aligned with Claude Code's setCwd().

        Tools that run shell commands call this to record a `cd`, so they never
        need access to RoleState. Delegates to the state controller.
        """
        self._state_ctl.set_cwd(path)

    def record_file_read(self, path: str, mtime_ns: int) -> None:
        """Record that a file was read, aligned with Claude Code's readFileState.

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

    def record_file_snapshot(self, full_path: str, *, tool: str = "") -> None:
        """Capture a before-image of a file a tool is about to overwrite.

        Delegates to the session's :attr:`file_snapshot_recorder`, which stores
        the prior on-disk content content-addressed and appends a snapshot event
        to the rollout log (the truth source for diff/undo). Ownership of the
        file-history sink lives in the Role; the Write/Edit/NotebookEdit tools
        call this capability without ever touching the session log directly.
        Best-effort — never raises into the tool.
        """
        self.file_snapshot_recorder.snapshot(full_path, tool=tool)

    def record_terminal_state(self, cwd: str, env: dict, unset: list, *, tool: str = "") -> None:
        """Record the persistent terminal's final cwd + env diff into the rollout.

        Delegates to the session's :attr:`terminal_state_recorder`, which appends
        a terminal-state event (last-write-wins) so a resumed session can re-seed
        a fresh shell to this state — without re-running any user commands. The
        Terminal tool calls this capability; best-effort, never raises.
        """
        self.terminal_state_recorder.record(cwd, env, unset, tool=tool)

    def take_pending_terminal_restore(self) -> Optional[dict]:
        """Return and clear the pending terminal-restore state ({cwd, env, unset}).

        Capability surface for the Terminal tool: when it starts a fresh shell it
        consumes the state staged by :meth:`resume_session` and re-seeds the
        shell once. Reading clears it so the restore happens exactly once.
        """
        value = self._state_ctl.get_pending_terminal_restore()
        if value is not None:
            self._state_ctl.set_pending_terminal_restore(None)
        return value

    def record_kernel_state(self, cwd: str, env: dict, unset: list, *, tool: str = "") -> None:
        """Record the persistent kernel's final cwd + env diff into the rollout.

        The Python sibling of :meth:`record_terminal_state`. Delegates to the
        session's :attr:`kernel_state_recorder`, which appends a kernel-state
        event (last-write-wins) so a resumed session can re-seed a fresh kernel
        to this state — without re-running any user code. The Python tool calls
        this capability; best-effort, never raises.
        """
        self.kernel_state_recorder.record(cwd, env, unset, tool=tool)

    def take_pending_kernel_restore(self) -> Optional[dict]:
        """Return and clear the pending kernel-restore state ({cwd, env, unset}).

        Capability surface for the Python tool: when it starts a fresh kernel it
        consumes the state staged by :meth:`resume_session` and re-seeds the
        kernel once. Reading clears it so the restore happens exactly once.
        """
        value = self._state_ctl.get_pending_kernel_restore()
        if value is not None:
            self._state_ctl.set_pending_kernel_restore(None)
        return value

    def record_browser_state(
        self,
        urls: list,
        *,
        active: int = 0,
        storage_state: Optional[dict] = None,
        tool: str = "",
    ) -> None:
        """Record the persistent browser's final tab URLs + session into the rollout.

        The browser sibling of :meth:`record_terminal_state`. Delegates to the
        session's :attr:`browser_state_recorder`, which appends a browser-state
        event (last-write-wins) so a resumed session can re-open the same tabs
        seeded with the saved session — without re-running any navigation/click
        actions. The WebBrowser tool calls this capability; best-effort, never
        raises. ``storage_state`` may carry cookies, so capture is gated by the
        recorder's ``enabled`` flag (the role's ``record_browser_state`` schema
        flag).
        """
        self.browser_state_recorder.record(urls, active=active, storage_state=storage_state, tool=tool)

    def take_pending_browser_restore(self) -> Optional[dict]:
        """Return and clear the pending browser-restore state.

        Capability surface for the WebBrowser tool: when it launches a fresh
        browser it consumes the state ({urls, active, storage_state}) staged by
        :meth:`resume_session` and re-opens the saved tabs once. Reading clears
        it so the restore happens exactly once.
        """
        value = self._state_ctl.get_pending_browser_restore()
        if value is not None:
            self._state_ctl.set_pending_browser_restore(None)
        return value

    def get_skill_pool(self):
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

    def tool_capabilities(self) -> dict[str, Any]:
        """The explicit allowlist of capabilities a tool may receive via bind().

        BaseTool.bind() resolves each name in a tool's `requires` against this
        map — and ONLY this map — so a tool can never reach RoleState, memory,
        or any Role attribute that is not deliberately published here. Adding a
        capability is an explicit decision; `getattr(role, ...)` is never used.
        """
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "deactivate": self.deactivate,
            "ask_human": self.ask_human,
            "get_bg_pool": self.get_bg_pool,
            "request_approval": self.request_approval,
            "reply_to_human": self.reply_to_human,
            "end_session": self.end_session,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "record_file_snapshot": self.record_file_snapshot,
            "record_terminal_state": self.record_terminal_state,
            "take_pending_terminal_restore": self.take_pending_terminal_restore,
            "record_kernel_state": self.record_kernel_state,
            "take_pending_kernel_restore": self.take_pending_kernel_restore,
            "record_browser_state": self.record_browser_state,
            "take_pending_browser_restore": self.take_pending_browser_restore,
            "get_browser_headless": self.get_browser_headless,
            "get_tool_session": self.get_tool_session,
            "set_tool_session": self.set_tool_session,
            "wait_interruptible": self.wait_interruptible,
            "get_skill_pool": self.get_skill_pool,
            "run_skill_fork": self.run_skill_fork,
            "get_sandbox_runtime": self.get_sandbox_runtime,
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
        as a tool→loop kill switch: the End tool and ask_human's "stop" call
        deactivate(), which must still break a loop that is mid-run.
        """
        self._state_ctl.set_active(value)

    def get_bg_pool(self) -> BackgroundTaskPool:
        """Return the background task pool (capability surface; delegates)."""
        return self._capabilities.get_bg_pool()

    def get_sandbox_runtime(self):
        """Return the OS-level sandbox runtime, or ``None`` when not configured.

        Capability surface for the command-execution tools (Bash / terminal /
        python). ``None`` when ``permissions.runtime`` is absent/disabled, in
        which case those tools run un-sandboxed (the historical behavior).
        """
        return self._components.sandbox_runtime

    def get_browser_headless(self) -> bool:
        """Return the role's ``browser_headless`` flag (True => run headless).

        Capability surface for the WebBrowser tool: lets the tool launch headed
        (a visible window) when the role opts in, without the executor layer
        reaching into the role schema. Defaults to True (headless).
        """
        return self.role_schema.browser_headless

    async def ask_human(self, question: str) -> str:
        """Ask the human user a question and return their response.

        Only valid inside an MGXEnv. A trailing 'stop' deactivates the role.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.ask_human(question)

    async def request_approval(self, prompt: str) -> str:
        """Ask the human to approve a tool call and return their raw reply.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.request_approval(prompt)

    async def reply_to_human(self, content: str) -> str:
        """Reply to the human user with the provided content.

        Only valid inside an MGXEnv. Capability surface; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.reply_to_human(content)

    async def wait_interruptible(self, duration_seconds: float) -> tuple[float, bool]:
        """Sleep for up to *duration_seconds*, waking early on activity.

        Capability surface for the Sleep tool; delegates to
        :class:`RoleCapabilities` (which owns the wait coordination).
        """
        return await self._capabilities.wait_interruptible(duration_seconds)

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

    async def _record_turn_context(self) -> None:
        """Persist this turn's save_to_context turn-context block into history.

        Renders the bus's persisted bucket (git status / token pressure / LSP
        diagnostics / ... — everything not flagged ``save_to_context=False``) and,
        when non-empty, appends it as a user message through the ContextManager so
        it is stored in history and recorded to the durable log. Best-effort:
        change-gated sources self-suppress, so quiet turns add nothing.
        """
        bus = self.turn_context_bus
        if bus is None:
            return
        block = await bus.collect_to_context(cwd=self.state.working_dir or None)
        if block:
            await self.context_manager.add(UserMessage(content=block))

    def _make_loop(self) -> BaseLoop:
        """Build the react-loop strategy for one run(), injecting components.

        Currently always a ReActLoop. Scatter-injects reusable components and
        plain callables only — never `self` and never a Role-private callback.
        The loop pulls its static LoopContext from the context_provider itself
        (provider.loop_context()), so the Role no longer hand-builds it here.
        Future: pick the loop class from role_schema (a registry); not yet.
        """
        return ReActLoop(
            think_engine=self.think_engine,
            command_channel=self.command_channel,
            executor=self.executor,
            memory=self.context_manager,
            context_provider=self.context_provider,
            is_active=self._is_active,
            set_active=self._set_active,
            get_bg_pool=self._peek_bg_pool,
        )

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
        is already written when the session_log was built. Also starts the opt-in
        external-file watcher (the property is None when disabled / no hook
        layer); its polling loop is stopped in :meth:`cleanup`.
        """
        if self._session_started:
            return
        self._session_started = True

        await self.event_bus.emit(
            SessionStartEvent(
                session_id=self.state.session_id,
                parent_session_id=self.state.parent_session_id,
                working_dir=self.state.working_dir,
                original_working_dir=self.state.original_working_dir,
                project_root=self.state.project_root,
                model=getattr(self.config.llm, "model", None),
                role_class=f"{type(self).__module__}.{type(self).__qualname__}",
                source="startup",
            )
        )

        watcher = self.file_watch_service
        if watcher is not None and not watcher.watcher.is_running():
            watcher.start()

    @role_raise_decorator
    async def run(self, with_message=None) -> Message | None:
        """Observe, and think and act based on the results of the observation"""
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

                # Persistent turn-context: the bus sources flagged save_to_context
                # (the default) are rendered once per turn and written into history
                # via the ContextManager, so they survive across turns / compaction
                # (vs the ephemeral request-only block in the user prompt).
                await self._record_turn_context()

                # Auto-continue budget (opt-in, default 0): a TurnEnd control
                # subscriber may block the "stop" to force another turn (CC's
                # Stop-hook semantics). The budget bounds it so a misbehaving
                # policy can never loop forever; with the default 0 (and no such
                # subscriber wired) the loop runs exactly once — byte-identical
                # to the old linear flow.
                auto_continue_budget = self.role_schema.max_auto_continue
                rsp = None
                while True:
                    loop = self._make_loop()
                    try:
                        rsp = await loop.run()
                    finally:
                        # Always propagate for recovery (role_raise_decorator reads it).
                        self.state.latest_observed_msg = loop.latest_observed_msg
                        # TurnEnd event: the recorder marks the turn boundary in the
                        # durable log (working_dir may have moved via `cd`, so capture
                        # the live value at turn end) and the HookSubscriber fires the
                        # Stop hook. Guarded on the slot so a failure before the bus was
                        # built never triggers lazy construction in teardown.
                        turn_outcome = await self._emit_turn_end()
                    if not self._should_auto_continue(turn_outcome, auto_continue_budget):
                        break
                    auto_continue_budget -= 1
                if rsp is None:
                    return None

                # Post-loop finalization (was Role.react): clear the active signal
                # and tag the response with this Role's display name.
                self._state_ctl.deactivate()
                if isinstance(rsp, AIMessage):
                    rsp.with_agent(self.role_schema.display_name)
                # Unify termination on "the end returns the rsp": the react loop's
                # terminal reply IS the run's result. The End tool (summary agents)
                # already populated ``last_end_output`` via ``end_session``; a native
                # terminal (no End, ``use_summary=False``) leaves it empty, so feed it
                # the terminal reply here. ``last_end_output`` is the single channel
                # the ephemeral spawn read-back (``ChildAgentHandle.result``) reads, so
                # a native child's output (e.g. a reviewer's final JSON) no longer
                # falls through the gap between the returned rsp and the read channel.
                if not self.state.last_end_output:
                    self.state.last_end_output = getattr(rsp, "content", "") or ""
                self.publish_message(rsp)
                return rsp

    @staticmethod
    def list_sessions(base_dir: str | None = None, *, cwd: str | None = None) -> list:
        """List resumable sessions (newest first); see ``session.list_sessions``.

        A thin, discoverable entry point onto the lite directory scan. ``cwd``
        filters to sessions started under that working dir / project root.
        """

        return _list(base_dir, cwd=cwd)

    def resume_session(self) -> bool:
        """Rebuild this role's stored history from its durable rollout log.

        The rollout (truth source) is replayed into ``state.context.messages``
        for the role's current ``session_id``. Returns False when no log exists
        (nothing to resume). On success the cwd/project anchors are restored from
        the session_meta and ``state.recovered`` is set.

        History is assigned straight into the backing context (not via
        ``ContextManager.add``), so the replayed messages are NOT re-recorded;
        the recorder stays live for messages added after resume, and
        ``SessionLog.create`` no-ops on the existing file (no duplicate meta).
        """

        log = SessionLog(self.state.session_id)
        if not log.exists():
            return False

        result = replay(log)  # replay scans via iter_raw, whose drain flushes queued writes first
        # Assign in place so the ContextManager (which backs onto this same list)
        # sees the rebuilt history without re-recording it.
        self.state.context.messages[:] = result.messages

        meta = result.meta or {}
        for field_name in ("working_dir", "original_working_dir", "project_root"):
            value = meta.get(field_name)
            if value:
                setattr(self.state, field_name, value)

        self.state.recovered = True
        # Stage the latest persistent-terminal state (if any) so the Terminal
        # tool re-seeds a fresh shell to it on first use — without re-running
        # any of the original commands.
        if result.terminal_state:
            self._state_ctl.set_pending_terminal_restore(result.terminal_state)
        # Likewise stage the latest persistent-kernel state so the Python tool
        # re-seeds a fresh kernel to it on first use (independent of the shell).
        if result.kernel_state:
            self._state_ctl.set_pending_kernel_restore(result.kernel_state)
        # Likewise stage the latest persistent-browser state so the WebBrowser
        # tool re-opens the saved tabs (seeded with the stored session) on first
        # use — without re-running any navigation/click actions.
        if result.browser_state:
            self._state_ctl.set_pending_browser_restore(result.browser_state)
        return True

    def fork_session(self) -> "Role":
        """Branch a sibling role off this session at its current history.

        Seeds a brand-new ``rollout.jsonl`` from this role's session (replayed to
        its final state) and records ``parent_session_id`` lineage on the child's
        ``session_meta``. Returns a fresh role of the same class, sharing the
        injected context/config, pinned to the new session and resumed onto the
        inherited history. The two sessions are independent afterwards: mutating
        the fork never touches this role's log.
        """

        child_id = fork(self.state.session_id)

        child_state = RoleState(
            session_id=child_id,
            parent_session_id=self.state.session_id,
            working_dir=self.state.working_dir,
            original_working_dir=self.state.original_working_dir,
            project_root=self.state.project_root,
        )
        forked = type(self)(
            role_schema=self.role_schema.model_copy(deep=True),
            state=child_state,
            context=self._context,
            config=self._config,
        )
        forked.resume_session()
        return forked

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
        (block the stop) to force another turn (CC Stop-hook semantics). Returns
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
                    model=getattr(self.config.llm, "model", None),
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

    # =========================================================================
    # Readiness
    # =========================================================================

    async def _ensure_ready(self):
        """Lazy init for expensive/fallible subsystems."""
        # Materialize the ContextManager (stored-history store + compaction
        # orchestrator), backed by RoleState.context so it survives recovery.
        _ = self.context_manager

        self.skill_manager.ensure_ready()
        await self.executor.init_mcp(self.role_schema.mcps)
