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

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional, Set

from metagpt.common.base import BaseRole
from metagpt.common.const import MESSAGE_ROUTE_TO_SELF
from metagpt.common.exception import RoleContextNotSetError
from metagpt.context import ContextManager
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.common.logs import bind_trace, log_class
from metagpt.common.observability.langfuse_integration import maybe_trace
from metagpt.think.prompt_builder import PromptBuilder
from metagpt.roles.context_provider import ContextProvider
from metagpt.loop import BaseLoop, ReActLoop
from metagpt.router.router import SUMMARY_TASK, LLMRouter
from metagpt.roles.role_components import RoleComponents, _resolve_shell_tools  # noqa: F401
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState, RoleStateController
from metagpt.common.schema import (
    AIMessage,
    CauseBy,
    Message,
    UserMessage,
)
from metagpt.context.skills.skill_manager import SkillManager
from metagpt.parser import CommandChannel
from metagpt.think.think_engine import ThinkEngine
from metagpt.executor.tasks import BackgroundTaskPool
from metagpt.common.utils.common import any_to_str, role_raise_decorator
from metagpt.common.utils.report import RecommendReporter, ThoughtReporter
from metagpt.common.utils.role_zero_utils import attach_media, detach_media

if TYPE_CHECKING:
    from metagpt.session import FileSnapshotRecorder, SessionLog


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
    def hook_manager(self):
        return self._components.hook_manager

    @property
    def lsp_service(self):
        return self._components.lsp_service

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
                {any_to_str(self), self.role_schema.name}
                if self.role_schema.name
                else {any_to_str(self)}
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
            "get_bg_pool": lambda: self.bg_pool,
            "request_approval": self.request_approval,
            "reply_to_human": self.reply_to_human,
            "end_session": self.end_session,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "record_file_snapshot": self.record_file_snapshot,
            "wait_interruptible": self.wait_interruptible,
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

    async def ask_human(self, question: str) -> str:
        """Ask the human user a question and return their response.

        Only valid inside an MGXEnv. A trailing 'stop' deactivates the role.
        """
        if not question:
            return "Error: 'question' argument is required."

        env = self.state.env
        if env is None:
            return "Not in MGXEnv, command will not be executed."

        response = await env.ask_human(question, sent_from=self.role_schema.name)
        if response.strip().lower().endswith(("stop", "<stop>")):
            response += " The user has asked me to stop because I have encountered a problem."
            self.deactivate()
        return response

    async def request_approval(self, prompt: str) -> str:
        """Ask the human to approve a tool call and return their raw reply.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Unlike ask_human(), this does NOT treat a trailing 'stop' as a kill
        switch — an approval prompt should never silently deactivate the Role.
        Outside an MGXEnv there is no channel, so it returns "" and the engine
        fails closed (denies).
        """
        env = self.state.env
        if env is None:
            return ""
        return await env.ask_human(prompt, sent_from=self.role_schema.name)

    async def reply_to_human(self, content: str) -> str:
        """Reply to the human user with the provided content.

        Only valid inside an MGXEnv.
        """
        if not content:
            return "Error: 'content' argument is required."

        env = self.state.env
        if env is None:
            return "Not in MGXEnv, command will not be executed."

        return await env.reply_to_human(content, sent_from=self.role_schema.name)

    async def wait_interruptible(self, duration_seconds: float) -> tuple[float, bool]:
        """Sleep for up to *duration_seconds*, waking early on activity.

        Wake conditions: a new message arrives in the message buffer (user
        input, background-task notification, etc.) or a background task
        completes. Owns the wait coordination so the Sleep tool stays a thin
        trigger and never touches RoleState, the msg_buffer, or the bg pool.

        Returns:
            (slept_seconds, interrupted) — elapsed time rounded to 0.1s and
            whether the sleep was cut short by activity.
        """
        msg_buffer = self.state.msg_buffer
        bg_pool = self._peek_bg_pool()

        start = time.time()
        sleep_task = asyncio.create_task(asyncio.sleep(duration_seconds))
        waiters = {sleep_task}

        msg_task = asyncio.create_task(msg_buffer.wait_for_message())
        waiters.add(msg_task)

        if bg_pool is not None:
            waiters.add(asyncio.create_task(bg_pool.wait_for_completion()))

        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            interrupted = sleep_task not in done
        finally:
            for t in waiters:
                t.cancel()

        return round(time.time() - start, 1), interrupted

    async def end_session(self) -> str:
        """End the current session and produce a summary if configured."""
        self._state_ctl.deactivate()

        memory = self.context_manager
        messages = memory.get(self.role_schema.memory_k)
        result = self.think_engine.result
        if not result.is_empty:
            messages = messages + [AIMessage(content=result.content)]
        messages = attach_media(messages)

        outputs = ""
        if self.role_schema.use_summary:
            need_recommend = self.role_schema.need_end_recommendations_tag
            reporter_cls = RecommendReporter if need_recommend else ThoughtReporter
            summary_prompt = PromptBuilder.pick_summary_prompt(
                summary_prompt=self.role_schema.summary_prompt,
                recommend_prompt=self.role_schema.summary_with_recommend_prompt,
                need_recommend=need_recommend,
            )
            summary_messages = messages + [UserMessage(content=summary_prompt)]
            # Peripheral (non-loop) call: routed via the "summary" task so it can
            # use a cheaper/different model (claude-sonnet-4-8) than the main llm.
            summary_llm = self.router.route_for_task(SUMMARY_TASK)
            async with reporter_cls() as reporter:
                await reporter.async_report({"type": "summary"})
                outputs = await summary_llm.aask(summary_messages)

        self.state.last_end_output = outputs
        detach_media(messages)
        return outputs

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

    @role_raise_decorator
    async def run(self, with_message=None) -> Message | None:
        """Observe, and think and act based on the results of the observation"""
        # Bind the session_id as the trace_id so every log line emitted during
        # this run (across the loop, think engine, executor, etc.) is correlated.
        # Bind the event bus to the async context so deep call sites (the LLM
        # client streaming tokens, a tool capturing a snapshot) can emit onto the
        # same spine without threading the bus through every signature.
        from metagpt.common.events import set_bus

        with bind_trace(self.session_id), set_bus(self.event_bus), maybe_trace(
            self.session_id, name=f"role.run:{self.name}"
        ):
            await self._ensure_ready()

            # SessionStart event: emitted once per Role across its run() calls.
            # The HookSubscriber fires the SessionStart hook; the recorder's meta
            # line is already written when the session_log was built.
            if not self._session_started:
                self._session_started = True
                from metagpt.common.events import SessionStartEvent

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

                # Start the external-file watcher once per session (opt-in; the
                # property returns None when disabled / no hook layer). It runs a
                # background polling loop and is stopped in cleanup().
                watcher = self.file_watch_service
                if watcher is not None and not watcher.watcher.is_running():
                    watcher.start()

            if with_message:
                msg = None
                if isinstance(with_message, str):
                    msg = Message(content=with_message)
                elif isinstance(with_message, Message):
                    msg = with_message
                elif isinstance(with_message, list):
                    msg = Message(content="\n".join(with_message))
                if not msg.cause_by:
                    msg.cause_by = CauseBy.USER_REQUIREMENT
                msg.send_to.add(self.role_schema.name)

                # UserPromptSubmit event: a subscriber (hook) may inject extra
                # context (prepended to the prompt) or veto the turn (stop ->
                # deactivate before loop). Emitting always; the folded outcome is
                # EMPTY when no hook layer is wired.
                from metagpt.common.events import UserPromptSubmitEvent

                outcome = await self.event_bus.emit(
                    UserPromptSubmitEvent(prompt=msg.content)
                )
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
                await self._emit_turn_end()
            if rsp is None:
                return None

            # Post-loop finalization (was Role.react): clear the active signal
            # and tag the response with this Role's display name.
            self._state_ctl.deactivate()
            if isinstance(rsp, AIMessage):
                rsp.with_agent(self.role_schema.display_name)
            self.publish_message(rsp)
            return rsp

    @staticmethod
    def list_sessions(base_dir: str | None = None, *, cwd: str | None = None) -> list:
        """List resumable sessions (newest first); see ``session.list_sessions``.

        A thin, discoverable entry point onto the lite directory scan. ``cwd``
        filters to sessions started under that working dir / project root.
        """
        from metagpt.session import list_sessions as _list

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
        from metagpt.session import SessionLog, replay

        log = SessionLog(self.state.session_id)
        if not log.exists():
            return False

        result = replay(log)
        # Assign in place so the ContextManager (which backs onto this same list)
        # sees the rebuilt history without re-recording it.
        self.state.context.messages[:] = result.messages

        meta = result.meta or {}
        for field_name in ("working_dir", "original_working_dir", "project_root"):
            value = meta.get(field_name)
            if value:
                setattr(self.state, field_name, value)

        self.state.recovered = True
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
        from metagpt.session import fork

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

    async def _emit_turn_end(self) -> None:
        """Emit ``TurnEndEvent`` delimiting one completed turn.

        Carries the per-turn runtime snapshot (working_dir may have moved via
        `cd`; token_state is optional metadata). The recorder subscriber maps it
        to a ``turn_context`` log record and the hook subscriber fires the Stop
        hook. Best-effort: skipped when the bus was never built (e.g. the run
        failed before _ensure_ready) and a failure never breaks the turn.
        """
        bus = self._components.peek_event_bus()
        if bus is None:
            return
        try:
            from dataclasses import asdict
            from uuid import uuid4

            from metagpt.common.events import TurnEndEvent

            token_state = None
            try:
                token_state = asdict(self.context_manager.token_state())
            except Exception:  # noqa: BLE001 — token math is optional metadata
                token_state = None
            await bus.emit(
                TurnEndEvent(
                    turn_id=uuid4().hex,
                    working_dir=self.state.working_dir,
                    model=getattr(self.config.llm, "model", None),
                    token_state=token_state,
                )
            )
        except Exception as exc:  # noqa: BLE001
            from metagpt.common.logs import logger

            logger.warning(f"session: failed to emit turn end: {exc}")

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
                from metagpt.common.logs import logger

                logger.warning(f"Role: file_watch_service.stop() failed: {exc}")
        lsp_service = self._components.peek_lsp_service()
        if lsp_service is not None:
            try:
                await lsp_service.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                from metagpt.common.logs import logger

                logger.warning(f"Role: lsp_service.shutdown() failed: {exc}")
        executor = self._components.peek_executor()
        if executor is not None:
            await executor.cleanup()

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


