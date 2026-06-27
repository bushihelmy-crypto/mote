#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RoleCapabilities — the rich tool-facing capability implementations.

Extracted from Role so the Role class stays focused on orchestration. Mirrors
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

from metagpt.common.agent_control import Lifecycle, SpawnContext, SpawnSpec, spawn_and_run
from metagpt.common.schema import AIMessage, UserMessage
from metagpt.common.utils.role_zero_utils import attach_media, detach_media
from metagpt.common.utils.report import RecommendReporter, ThoughtReporter
from metagpt.router.router import SUMMARY_TASK
from metagpt.roles.role_state import RoleState
from metagpt.think.prompt_builder import PromptBuilder

if TYPE_CHECKING:
    from metagpt.executor.tasks import BackgroundTaskPool
    from metagpt.context.skills.skill_pool import SkillPool
    from metagpt.roles.role import Role


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
        child_schema.instruction = instructions
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
                child_config.llm.model = model
                if effort:
                    child_config.llm.reasoning_effort = effort
            except Exception:  # noqa: BLE001 — model override is best-effort
                child_config = role._config

        child_state = RoleState(
            parent_session_id=role.state.session_id,
            working_dir=role.get_cwd(),
        )

        # Born on the plane through the single spawn authority (resolved via the
        # explicit ``ctx.agent_control`` on our shared Context), so the fork
        # counts against the cap / joins the lineage tree like any other child.
        # The factory still shares our Context (so cost rolls up to us — the
        # skill-fork contract), and the handle always tears the child down (its
        # own terminal/kernel PTY, LSP servers, file-watch loop are session-
        # scoped OS resources that leak if dropped without cleanup()).
        def role_factory(spawn_ctx: SpawnContext):
            return type(role)(
                role_schema=child_schema,
                state=child_state,
                context=role._context,
                config=child_config,
            )

        spec = SpawnSpec(
            role_factory=role_factory,
            nickname="skill_fork",
            agent_role="skill_fork",
            parent_id=role.state.session_id,
            lifecycle=Lifecycle.EPHEMERAL,
        )
        report = await spawn_and_run(spec, UserMessage(content=arguments), ctx=role._context)
        if report is None:
            return "Error: could not run skill fork (agent limit reached)."
        return report.strip()

    # ------------------------------------------------------------------
    # Human I/O (only valid inside an MGXEnv)
    # ------------------------------------------------------------------

    async def ask_human(self, question: str) -> str:
        """Ask the human user a question and return their response.

        Only valid inside an MGXEnv. A trailing 'stop' deactivates the role.
        """
        if not question:
            return "Error: 'question' argument is required."

        role = self._role
        env = role.state.env
        if env is None:
            return "Not in MGXEnv, command will not be executed."

        response = await env.ask_human(question, sent_from=role.role_schema.name)
        if response.strip().lower().endswith(("stop", "<stop>")):
            response += " The user has asked me to stop because I have encountered a problem."
            role.deactivate()
        return response

    async def request_approval(self, prompt: str) -> str:
        """Ask the human to approve a tool call and return their raw reply.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Unlike ask_human(), this does NOT treat a trailing 'stop' as a kill
        switch — an approval prompt should never silently deactivate the Role.
        Outside an MGXEnv there is no channel, so it returns "" and the engine
        fails closed (denies).
        """
        role = self._role
        env = role.state.env
        if env is None:
            return ""
        return await env.ask_human(prompt, sent_from=role.role_schema.name)

    async def reply_to_human(self, content: str) -> str:
        """Reply to the human user with the provided content.

        Only valid inside an MGXEnv.
        """
        if not content:
            return "Error: 'content' argument is required."

        role = self._role
        env = role.state.env
        if env is None:
            return "Not in MGXEnv, command will not be executed."

        return await env.reply_to_human(content, sent_from=role.role_schema.name)

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
        role = self._role
        role.deactivate()

        memory = role.context_manager
        messages = memory.get(role.role_schema.memory_k)
        result = role.think_engine.result
        if not result.is_empty:
            messages = messages + [AIMessage(content=result.content)]
        messages = attach_media(messages)

        outputs = ""
        if role.role_schema.use_summary:
            need_recommend = role.role_schema.need_end_recommendations_tag
            reporter_cls = RecommendReporter if need_recommend else ThoughtReporter
            summary_prompt = PromptBuilder.pick_summary_prompt(
                summary_prompt=role.role_schema.summary_prompt,
                recommend_prompt=role.role_schema.summary_with_recommend_prompt,
                need_recommend=need_recommend,
            )
            summary_messages = messages + [UserMessage(content=summary_prompt)]
            # Peripheral (non-loop) call: routed via the "summary" task so it can
            # use a cheaper/different model than the main llm.
            summary_llm = role.router.route_for_task(SUMMARY_TASK)
            async with reporter_cls() as reporter:
                await reporter.async_report({"type": "summary"})
                outputs = await summary_llm.aask(summary_messages)

        role.state.last_end_output = outputs
        detach_media(messages)
        return outputs
