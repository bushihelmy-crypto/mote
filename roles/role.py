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
from typing import Any, Optional, Set

from metagpt.common.base import BaseRole
from metagpt.common.const import MESSAGE_ROUTE_TO_SELF
from metagpt.common.exception import RoleContextNotSetError
from metagpt.context import ContextManager
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.common.logs import bind_trace, log_class
from metagpt.common.observability.langfuse_integration import maybe_trace
from metagpt.prompts.prompt_builder import PromptBuilder
from metagpt.roles.context_provider import ContextProvider
from metagpt.loop import BaseLoop, ReActLoop
from metagpt.router.router import COMPRESSION_TASK, SUMMARY_TASK, LLMRouter, get_router
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState
from metagpt.common.schema import (
    AIMessage,
    CauseBy,
    Message,
    UserMessage,
)
from metagpt.skills.skill_manager import SkillManager
from metagpt.parser import (
    CommandChannel,
    infer_native_tool_provider,
    make_command_channel,
)
from metagpt.think.think_engine import ThinkEngine
from metagpt.tasks import BackgroundTaskPool
from metagpt.common.utils.common import any_to_str, role_raise_decorator
from metagpt.common.utils.report import RecommendReporter, ThoughtReporter
from metagpt.common.utils.role_zero_utils import attach_media, detach_media


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

        # External dependencies (injected)
        self._context = context
        self._config = config

        # Lazy-init component slots
        self._think_engine: Optional[ThinkEngine] = None
        self._executor: Optional[ToolExecutor] = None
        self._skill_mgr: Optional[SkillManager] = None
        self._bg_pool: Optional[BackgroundTaskPool] = None
        self._command_channel: Optional[CommandChannel] = None
        self._context_provider: Optional[ContextProvider] = None
        self._context_manager: Optional[ContextManager] = None
        self._router: Optional[LLMRouter] = None

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
    def router(self) -> LLMRouter:
        """The LLM router bound to this Role's context (lazy-init, cached).

        The Role no longer holds a single LLM. It holds the router and passes it
        down; whoever needs an LLM resolves one through the router on demand (the
        react loop, via the ContextProvider, triggers it per request). Built once
        over ``self.context`` so its model registry + instance cache (and the
        FALLBACK-recovery supplier wired onto each provider) stay consistent.
        """
        if self._router is None:
            self._router = get_router(self.context)
        return self._router

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

    @property
    def skill_manager(self) -> SkillManager:
        if self._skill_mgr is None:
            self._skill_mgr = SkillManager(skills=self.role_schema.skills)
        return self._skill_mgr

    @property
    def bg_pool(self) -> BackgroundTaskPool:
        if self._bg_pool is None:
            self._bg_pool = BackgroundTaskPool(msg_buffer=self.state.msg_buffer)
        return self._bg_pool

    def _peek_bg_pool(self) -> Optional[BackgroundTaskPool]:
        """Return the background pool only if a tool already created it.

        Never lazily constructs one (unlike the ``bg_pool`` property): the loop
        and ``wait_interruptible`` only ever inspect pending state / await
        completion, so materializing a pool just to peek would be wasteful. A
        named accessor instead of reading the ``_bg_pool`` slot directly so the
        "peek, don't create" intent is explicit at every call site.
        """
        return self._bg_pool

    @property
    def executor(self) -> ToolExecutor:
        if self._executor is None:
            all_tools = self.role_schema.mcps + self.role_schema.tools
            self._executor = ToolExecutor(
                session_id=self.state.session_id,
                tools=all_tools,
                role=self,
                permission_config=self.role_schema.permissions,
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
                self.state.context,
                llm=self.router.route_for_task(COMPRESSION_TASK),
                model=getattr(self.config.llm, "model", None),
            )
        return self._context_manager

    @property
    def think_engine(self) -> ThinkEngine:
        if self._think_engine is None:
            self._think_engine = ThinkEngine(
                memory=self.context_manager, config=self.config,
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
                self.role_schema.command_protocol,
                provider=infer_native_tool_provider(self.config.llm),
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
            self._context_provider = ContextProvider(self)
        return self._context_provider

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
        return self.state.msg_buffer.empty()

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

        Returns the live cwd (state.working_dir), falling back to the startup
        directory. Wrapped in try/except so it never returns an empty string.
        """
        try:
            return self.state.working_dir or self.state.original_working_dir
        except Exception:
            return self.state.original_working_dir

    def set_cwd(self, path: str) -> None:
        """Persist the live working directory, aligned with Claude Code's setCwd().

        Cwd ownership lives in the Role (on RoleState), not in tools. Tools that
        run shell commands call this to record a `cd`, so they never need access
        to RoleState (and therefore never touch memory).
        """
        self.state.working_dir = path

    def record_file_read(self, path: str, mtime_ns: int) -> None:
        """Record that a file was read, aligned with Claude Code's readFileState.

        Ownership of the shared file-read state lives in the Role (on RoleState),
        not in tools. The Read tool calls this after a successful read so that
        the Write/Edit tools can later enforce read-before-overwrite and detect
        external modifications — without ever touching RoleState directly.
        """
        self.state._file_read_state[path] = mtime_ns

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        """Return the mtime_ns recorded when `path` was last read, else None.

        Counterpart to record_file_read(). The Write/Edit tools compare the
        returned value against the file's current mtime to decide whether the
        model has seen the latest content before overwriting it.
        """
        return self.state._file_read_state.get(path)

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
            "request_approval": self.request_approval,
            "reply_to_human": self.reply_to_human,
            "end_session": self.end_session,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "wait_interruptible": self.wait_interruptible,
        }

    def deactivate(self) -> None:
        """Stop the react loop after the current step."""
        self.state._active = False

    def _is_active(self) -> bool:
        """Read the shared active signal (consumed by the loop's think step)."""
        return self.state._active

    def _set_active(self, value: bool) -> None:
        """Write the shared active signal (used by the loop each iteration).

        `active` lives on RoleState — not inside the loop — because it doubles
        as a tool→loop kill switch: the End tool and ask_human's "stop" call
        deactivate(), which must still break a loop that is mid-run.
        """
        self.state._active = value

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
        self.state._active = False

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
        if not message:
            return
        self.state.msg_buffer.push(message)

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
        with bind_trace(self.session_id), maybe_trace(self.session_id, name=f"role.run:{self.name}"):
            await self._ensure_ready()

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
                self.put_message(msg)

            loop = self._make_loop()
            try:
                rsp = await loop.run()
            finally:
                # Always propagate for recovery (role_raise_decorator reads it).
                self.state.latest_observed_msg = loop.latest_observed_msg
            if rsp is None:
                return None

            # Post-loop finalization (was Role.react): clear the active signal
            # and tag the response with this Role's display name.
            self.state._active = False
            if isinstance(rsp, AIMessage):
                rsp.with_agent(self.role_schema.display_name)
            self.publish_message(rsp)
            return rsp

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


