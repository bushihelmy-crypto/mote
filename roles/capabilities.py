#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RoleCapabilities — the rich tool-facing capability implementations.

Owns the rich capability behaviour so the Role class stays focused on orchestration. Mirrors
:class:`RoleStateController`: this holder owns the *behaviour* behind the
capabilities that need the Role's subsystems (human I/O via the env,
interruptible sleep, the end-of-session summary, skill forks, the background
task pool, the skill pool), and the Role keeps thin delegators onto these
methods as its capability surface (the ``tool_capabilities()`` allowlist).

The thin state delegators (cwd, file-read map, tool-session store) stay on
:class:`RoleStateController` — this holder covers only the capabilities that
reach into the Role's *subsystems* rather than just its serializable state.

Holds the Role by reference and only READS it (env, subsystems, schema); it
never reassigns Role state.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from mote.common.agent_control import ContextPolicy, Lifecycle, SpawnContext, SpawnSpec, spawn_and_run
from mote.common.schema import UserMessage
from mote.roles.role_state import RoleState

if TYPE_CHECKING:
    from mote.context.skills.skill_pool import SkillPool
    from mote.executor.tasks import BackgroundTaskPool
    from mote.roles.role import Role


# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_SKILL_FORK_FAILED = "Error: could not run skill fork (agent limit reached)."
_MSG_QUESTION_REQUIRED = "Error: 'question' argument is required."
_MSG_CONTENT_REQUIRED = "Error: 'content' argument is required."
_MSG_NOT_IN_MOTE_ENV = "Not in MoteEnv, command will not be executed."
_MSG_STOP_SUFFIX = " The user has asked me to stop because I have encountered a problem."


class RoleCapabilities:
    """Behaviour behind the Role's subsystem-backed tool capabilities."""

    def __init__(self, role: "Role"):
        self._role = role

    # ------------------------------------------------------------------
    # Background tasks / skills
    # ------------------------------------------------------------------

    def get_bg_pool(self) -> "BackgroundTaskPool":
        """Return the background task pool, building it on first use.

        Capability surface for the task tools (cancel/resume/status); they reach
        the pool through this instead of touching RoleComponents directly.
        """
        return self._role.bg_pool

    def get_skill_pool(self) -> Optional["SkillPool"]:
        """Return the live :class:`SkillPool`, or None when skills are disabled.

        Capability surface for the ``Skill`` bridge tool: it resolves the loaded
        skill pool (so it can look skills up by name, render them, or search the
        long tail) without ever reaching into the SkillManager directly. Returns
        None when no skills are configured — the tool reports that itself.
        """
        return self._role.skill_manager.pool

    async def run_skill_fork(
        self,
        *,
        instructions: str,
        arguments: str = "",
        allowed_tools: Optional[list] = None,
        model: str = "",
        effort: str = "",
    ) -> str:
        """Run a ``context: fork`` skill inside a fresh, isolated child Role.

        Mirrors the :class:`Agent` tool / :meth:`Role.fork_session`: a brand-new
        Role is built whose system prompt carries the rendered skill body
        (``instructions``) and whose tools are limited to ``allowed_tools`` (the
        SKILL.md ``allowed-tools`` allowlist — the natural capability fence, no
        PermissionEngine overlay needed). The child does NOT inherit this Role's
        conversation history (a fresh ``RoleState`` keyed by a new session id,
        with ``parent_session_id`` lineage), so the long skill process never
        pollutes the main transcript. Only the child's final summary returns.
        """
        role = self._role
        child_schema = role.role_schema.model_copy(deep=True)
        # Carry the rendered skill body into the child's system prompt (the only
        # channel that reaches the model — see RoleSchema identity notes).
        child_schema.system_prompt = f"{child_schema.system_prompt}\n\n{instructions}"
        child_schema.tools = list(allowed_tools or [])
        # A fork skill is a bounded subtask, not a delegating orchestrator: drop
        # MCP/agent/skill declarations so it cannot spawn its own children.
        child_schema.mcps = []
        child_schema.agents = []
        child_schema.skills = []

        child_config = role._config
        if model and child_config is not None:
            try:
                child_config = child_config.model_copy(deep=True)
                child_config.models.default.model = model
                if effort:
                    child_config.models.default.reasoning_effort = effort
            except Exception:  # noqa: BLE001 — model override is best-effort
                child_config = role._config

        child_state = RoleState(
            parent_session_id=role.state.session_id,
            working_dir=role.get_cwd(),
        )

        # Born on the plane through the single spawn authority (resolved via the
        # explicit ``ctx.agent_control`` on our shared Context), so the fork
        # counts against the cap / joins the lineage tree like any other child.
        # We declare SHARE_PARENT so the authority hands the child *our* Context
        # (cost rolls up to us — the skill-fork contract); the factory itself no
        # longer touches context. The handle always tears the child down (its own
        # terminal/kernel PTY, LSP servers, file-watch loop are session-scoped OS
        # resources that leak if dropped without cleanup()).
        def role_factory(spawn_ctx: SpawnContext):
            return type(role)(
                role_schema=child_schema,
                state=child_state,
                config=child_config,
            )

        spec = SpawnSpec(
            role_factory=role_factory,
            nickname="skill_fork",
            agent_role="skill_fork",
            parent_id=role.state.session_id,
            lifecycle=Lifecycle.EPHEMERAL,
            context_policy=ContextPolicy.SHARE_PARENT,
        )
        report = await spawn_and_run(spec, UserMessage(content=arguments), ctx=role._context)
        if report is None:
            return _MSG_SKILL_FORK_FAILED
        return report.strip()

    # ------------------------------------------------------------------
    # Session-log-backed capture (before-image snapshots + persistent state)
    # ------------------------------------------------------------------

    def record_file_snapshot(self, full_path: str, *, tool: str = "") -> None:
        """Capture a before-image of a file a tool is about to overwrite.

        Delegates to the session's ``file_snapshot_recorder``, which stores the
        prior on-disk content content-addressed and appends a snapshot event to
        the rollout log (the truth source for diff/undo). The Write/Edit tools
        call this capability without touching the session log directly.
        Best-effort — never raises into the tool.
        """
        self._role.file_snapshot_recorder.snapshot(full_path, tool=tool)

    def register_resource(self, *, id: str, kind: str, content: str) -> None:
        """Register a loaded capability body for post-compaction re-projection.

        Delegates to the Role's ``resource_registry``. The Skill tool calls this
        after rendering an inline skill body so the body is re-projected after an
        autocompaction discards the head. Best-effort — the registry's ``load``
        is a plain dict write and does not raise.
        """
        self._role.resource_registry.load(id=id, kind=kind, content=content)

    def record_terminal_state(self, cwd: str, env: dict, unset: list, *, tool: str = "") -> None:
        """Record the persistent terminal's final cwd + env diff into the rollout.

        Delegates to the session's ``terminal_state_recorder``, which appends a
        terminal-state event (last-write-wins) so a resumed session can re-seed a
        fresh shell to this state — without re-running any user commands. The
        Terminal tool calls this capability; best-effort, never raises.
        """
        self._role.terminal_state_recorder.record(cwd, env, unset, tool=tool)

    def record_kernel_state(self, cwd: str, env: dict, unset: list, *, tool: str = "") -> None:
        """Record the persistent kernel's final cwd + env diff into the rollout.

        The Python sibling of :meth:`record_terminal_state`. Delegates to the
        session's ``kernel_state_recorder`` (last-write-wins) so a resumed session
        can re-seed a fresh kernel to this state — without re-running any user
        code. The Python tool calls this capability; best-effort, never raises.
        """
        self._role.kernel_state_recorder.record(cwd, env, unset, tool=tool)

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
        session's ``browser_state_recorder`` (last-write-wins) so a resumed
        session can re-open the same tabs seeded with the saved session — without
        re-running any navigation/click actions. ``storage_state`` may carry
        cookies, so capture is gated by the recorder's ``enabled`` flag. The
        WebBrowser tool calls this capability; best-effort, never raises.
        """
        self._role.browser_state_recorder.record(urls, active=active, storage_state=storage_state, tool=tool)

    # ------------------------------------------------------------------
    # Human I/O (only valid inside a MoteEnv)
    # ------------------------------------------------------------------

    async def ask_user(self, question: str) -> str:
        """Ask the user a question and return their response.

        Only valid inside a MoteEnv. A trailing 'stop' deactivates the role.
        """
        if not question:
            return _MSG_QUESTION_REQUIRED

        role = self._role
        env = role.state.env
        if env is None:
            return _MSG_NOT_IN_MOTE_ENV

        response = await env.ask_user(question, sent_from=role.role_schema.name)
        if response.strip().lower().endswith(("stop", "<stop>")):
            response += _MSG_STOP_SUFFIX
            role.deactivate()
        return response

    async def ask_user_question(self, questions):
        """Ask the user structured multiple-choice questions; return structured answers.

        The structured sibling of :meth:`ask_user` behind the ``AskUserQuestion``
        tool. Deliberately does NOT apply ask_user's trailing 'stop' → deactivate
        kill switch: a structured selection is not a control channel, so the stop
        semantics stay on the plain ``ask_user`` / ``AskUser`` path.
        """
        from mote.common.schema import AskUserQuestionAnswers

        role = self._role
        env = role.state.env
        if env is None:
            return AskUserQuestionAnswers()
        return await env.ask_user_question(questions, sent_from=role.role_schema.name)

    async def request_approval(self, prompt: str) -> str:
        """Ask the human to approve a tool call and return their raw reply.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Unlike ask_user(), this does NOT treat a trailing 'stop' as a kill
        switch — an approval prompt should never silently deactivate the Role.
        Outside a MoteEnv there is no channel, so it returns "" and the engine
        fails closed (denies).
        """
        role = self._role
        env = role.state.env
        if env is None:
            return ""
        return await env.ask_user(prompt, sent_from=role.role_schema.name)

    async def reply_to_user(self, content: str) -> str:
        """Reply to the user with the provided content.

        Only valid inside a MoteEnv.
        """
        if not content:
            return _MSG_CONTENT_REQUIRED

        role = self._role
        env = role.state.env
        if env is None:
            return _MSG_NOT_IN_MOTE_ENV

        return await env.reply_to_user(content, sent_from=role.role_schema.name)

    # ------------------------------------------------------------------
    # Loop coordination
    # ------------------------------------------------------------------

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
        role = self._role
        msg_buffer = role.state.msg_buffer
        bg_pool = role._peek_bg_pool()

        start = time.time()
        sleep_task = asyncio.create_task(asyncio.sleep(duration_seconds))
        waiters: set[asyncio.Task] = {sleep_task}

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
        """End the current session.

        Deactivates the Role so the run loop terminates. The terminal reply is
        captured into ``state.last_end_output`` by the run loop's post-loop
        finalization (see Role.run), which is the single channel an ephemeral
        spawn's read-back reads — so returning "" here is fine.
        """
        self._role.deactivate()
        return ""
