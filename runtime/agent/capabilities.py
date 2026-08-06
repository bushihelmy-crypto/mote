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
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar

from pydantic import BaseModel

from mote.contracts.agent import (
    AgentBuilder,
    AgentConstructionRequest,
    ContextPolicy,
    Lifecycle,
    RunnableAgent,
    SpawnableAgentDefinition,
    SpawnPlan,
)
from mote.contracts.conversation import UserMessage
from mote.contracts.interaction import ApprovalChoice, AskUserQuestionAnswers
from mote.contracts.interaction.handoff import HandoffOutcome, HandoffRequest, HandoffStatus
from mote.contracts.model.topology_codec import decode_route_id
from mote.contracts.output import RunResult
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.task.models import TaskId, TaskResultRecord
from mote.kernel.output import text_output_contract
from mote.runtime.agent.control import spawn_and_run
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring
from mote.runtime.interactive.handoff import HandoffCoordinator
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.timers import SessionTimerState, SessionTimerStore
from mote.runtime.tools.execution_context import current_authorized_invocation

if TYPE_CHECKING:
    from mote.contracts.file import (
        EditCommitOutcome,
        FileByteView,
        FileSnapshot,
        FileTextView,
        MutationResult,
        PdfView,
        ReadRequest,
        SearchOutputMode,
        SearchResult,
    )
    from mote.contracts.interaction import ApprovalRequest
    from mote.contracts.ports.skill.registry import SkillCatalog
    from mote.runtime.agent.role import Role
    from mote.runtime.agent.role_schema import RoleSchema
    from mote.runtime.fileops.edit_plans import EditPlan, EditPlanRequest


# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_SKILL_FORK_FAILED = "Error: could not run skill fork (agent limit reached)."
_MSG_QUESTION_REQUIRED = "Error: 'question' argument is required."
_MSG_CONTENT_REQUIRED = "Error: 'content' argument is required."
_MSG_NO_HUMAN_INTERACTION = "No human interaction channel is bound; command will not be executed."
_MSG_STOP_SUFFIX = " The user has asked me to stop because I have encountered a problem."


SkillDepsT = TypeVar("SkillDepsT")


class _SkillForkBuilder(AgentBuilder[AgentConstructionRequest, str], Generic[SkillDepsT]):
    def __init__(
        self,
        role_type: type["Role[SkillDepsT, str]"],
        child_schema: "RoleSchema",
        child_state: RoleState,
        child_config: BaseModel | None,
        wiring: "AgentWiring[SkillDepsT, str]",
    ) -> None:
        self._role_type = role_type
        self._child_schema = child_schema
        self._child_state = child_state
        self._child_config = child_config
        self._wiring = wiring

    def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]:
        self._child_state.session_id = request.logical_agent_id
        child = self._role_type(
            role_schema=self._child_schema,
            state=self._child_state,
            config=self._child_config,
            wiring=self._wiring,
        )
        return child


class RoleCapabilities:
    """Behaviour behind the Role's subsystem-backed tool capabilities."""

    def __init__(self, role: "Role"):
        self._role = role
        self._timer_store: SessionTimerStore | None = None

    # ------------------------------------------------------------------
    # Background tasks / skills
    # ------------------------------------------------------------------

    def get_bg_pool(self) -> BackgroundTaskService:
        """Return the background task pool, building it on first use.

        Capability surface for the task tools (cancel/resume/status); they reach
        the pool through this instead of touching RoleComponents directly.
        """
        return self._role.bg_pool

    def get_skill_pool(self) -> Optional["SkillCatalog"]:
        """Return the live Skill catalog, or None when skills are disabled.

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
        allowed_tools: Optional[list[str]] = None,
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
        parent_schema = role.role_schema
        requested_tools = tuple(allowed_tools or ())
        parent_tools = frozenset((*parent_schema.tools, *parent_schema.deferred_tools))
        unauthorized = set(requested_tools) - parent_tools
        if unauthorized:
            raise ValueError(f"skill fork cannot expand parent tools: {sorted(unauthorized)}")
        route_id = parent_schema.model_route
        if model:
            route_id = decode_route_id(model)
            composition = role._components.current_runtime_composition()
            if not composition.route_policy.supports(route_id):
                raise ValueError(f"skill fork route is unavailable: {model!r}")
        # Produce one immutable deployment projection. A fork skill is a bounded
        # subtask, so MCP/agent/skill declarations are removed atomically with
        # the prompt, tool allowlist, and optional route specialization.
        child_schema = parent_schema.model_copy(
            deep=True,
            update={
                "system_prompt": f"{parent_schema.system_prompt}\n\n{instructions}",
                "tools": list(requested_tools),
                "mcps": [],
                "agents": [],
                "skills": [],
                "model_route": route_id,
            },
        )
        child_config = role._config
        child_wiring = AgentWiring(dependencies=role._wiring.dependencies.with_output_contract(text_output_contract()))

        child_state = RoleState(
            parent_session_id=role.state.session_id,
            working_dir=role.get_cwd(),
        )

        # Born on the plane through the single spawn authority (resolved via the
        # scheduler-bound ambient AgentControlPort), so the fork
        # counts against the cap / joins the lineage tree like any other child.
        # We declare SHARE_PARENT so the authority hands the child *our* Context
        # (cost rolls up to us — the skill-fork contract); the factory itself no
        # longer touches context. The handle always tears the child down (its own
        # terminal/kernel PTY, LSP servers, file-watch loop are session-scoped OS
        # resources that leak if dropped without cleanup()).
        invocation = current_authorized_invocation()
        request_id = uuid.uuid4().hex if invocation is None else str(invocation.identity.invocation_id)
        spec = SpawnPlan(
            request_id=request_id,
            definition=SpawnableAgentDefinition(
                name="skill_fork",
                aliases=(),
                description="Run one isolated skill fork.",
                version="1",
                builder=_SkillForkBuilder(type(role), child_schema, child_state, child_config, child_wiring),
            ),
            nickname="skill_fork",
            agent_role="skill_fork",
            parent_id=role.state.session_id,
            lifecycle=Lifecycle.EPHEMERAL,
            context_policy=ContextPolicy.SHARE_PARENT,
        )
        report = await spawn_and_run(spec, UserMessage(content=arguments))
        if not isinstance(report, RunResult):
            return _MSG_SKILL_FORK_FAILED
        return str(report.output).strip()

    # ------------------------------------------------------------------
    # Session-log-backed capture (before-image snapshots + persistent state)
    # ------------------------------------------------------------------

    def capture_file_snapshot(
        self,
        full_path: str,
        *,
        encoding: str | None = None,
        fallback_encoding: str | None = None,
    ):
        return self._role._file_operations.capture(
            full_path,
            encoding=encoding,
            fallback_encoding=fallback_encoding,
        )

    def observe_file_snapshot(self, snapshot: "FileSnapshot") -> None:
        self._role._file_operations.observe(snapshot)

    def read_file_view(
        self,
        full_path: str,
        request: "ReadRequest",
    ) -> "FileByteView | FileTextView | PdfView":
        return self._role._file_operations.read_view(full_path, request)

    def search_files(
        self,
        *,
        root: str,
        content: str = "",
        files: str = "",
        type_name: str = "",
        output_mode: "SearchOutputMode",
        case_insensitive: bool = False,
        before_context: int = 0,
        after_context: int = 0,
        multiline: bool = False,
        encoding: str | None = None,
        fallback_encoding: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        cursor: str | None = None,
        timeout: float = 20.0,
    ) -> "SearchResult":
        return self._role._file_operations.search(
            root=root,
            content=content,
            files=files,
            type_name=type_name,
            output_mode=output_mode,
            case_insensitive=case_insensitive,
            before_context=before_context,
            after_context=after_context,
            multiline=multiline,
            encoding=encoding,
            fallback_encoding=fallback_encoding,
            limit=limit,
            offset=offset,
            cursor=cursor,
            timeout=timeout,
        )

    async def plan_file_edit(self, request: "EditPlanRequest") -> "EditPlan":
        return await run_disk_io(
            self._role._file_operations.plan_file_edit,
            request,
        )

    async def commit_edit_plan(self, plan_id: str) -> "EditCommitOutcome":
        review_turn_index = self._role.current_turn_index() if self._role.role_schema.record_hunks else None
        return await run_disk_io(
            self._role._file_operations.commit_edit_plan,
            plan_id,
            review_turn_index=review_turn_index,
        )

    async def commit_generated_files(
        self,
        files: dict[str, bytes],
        *,
        source: str,
        transaction_id: str | None = None,
    ) -> "MutationResult":
        return await run_disk_io(
            self._role._file_operations.commit_generated_files,
            files,
            source=source,
            transaction_id=transaction_id,
        )

    def try_reserve_generated_targets(self, targets: tuple[str, ...]):
        return self._role._file_operations.try_reserve_generated_targets(targets)

    def register_resource(self, *, id: str, kind: str, content: str) -> None:
        """Register a loaded capability body for post-compaction re-projection.

        Delegates to the Role's ``resource_registry``. The Skill tool calls this
        after rendering an inline skill body so the body is re-projected after an
        autocompaction discards the head. Best-effort — the registry's ``load``
        is a plain dict write and does not raise.
        """
        self._role._components.resource_registry.load(id=id, kind=kind, content=content)

    def register_task_result(self, task_id: TaskId, content: str) -> None:
        """Register a background task's push-once result for re-projection.

        The pool's terminal callback calls this so a graph terminal / agent
        summary / pause marker survives an autocompaction that discards the live
        ``BackgroundTaskNotification`` — the registry re-projects the ``content``
        pointer right after the summary until the model consumes (and unloads)
        it or the round cap recycles it. Best-effort dict write; does not raise.
        """
        self._role._components.resource_registry.load(id=task_id, kind="task_result", content=content, sticky=True)

    def unload(self, task_id: TaskId) -> TaskResultRecord | None:
        content = self._role._components.resource_registry.unload_content(task_id)
        return TaskResultRecord(task_id, content) if content is not None else None

    # ------------------------------------------------------------------
    # Human I/O (available only while an explicit interaction Port is bound)
    # ------------------------------------------------------------------

    async def ask_user(self, question: str) -> str:
        """Ask the user a question and return their response.

        Requires a bound interaction Port. A trailing 'stop' deactivates the role.
        """
        if not question:
            return _MSG_QUESTION_REQUIRED

        role = self._role
        env = role.human_interaction
        if env is None:
            return _MSG_NO_HUMAN_INTERACTION

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
        role = self._role
        env = role.human_interaction
        if env is None:
            return AskUserQuestionAnswers()
        return await env.ask_user_question(questions, sent_from=role.role_schema.name)

    async def handoff_runtime(self, runtime: str, *, message: str = "") -> HandoffOutcome:
        """Transfer one managed Runtime's exclusive writer ownership to the human."""
        role = self._role
        host = role._components.runtime_host
        descriptor = host.descriptor(runtime)
        env = role.human_interaction
        if env is None:
            return HandoffOutcome(
                status=HandoffStatus.UNAVAILABLE,
                runtime_ref=descriptor.ref,
                from_revision=descriptor.revision,
                to_revision=descriptor.revision,
            )

        request = HandoffRequest(runtime_ref=descriptor.ref, message=message)
        return await HandoffCoordinator(host, env).handoff(
            request,
            owner_id=f"human:{role.session_id}",
            expected_revision=descriptor.revision,
        )

    async def request_approval(self, request: "ApprovalRequest") -> "ApprovalChoice":
        """Ask the human to approve a gated tool call; return their decision.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Takes a language-neutral :class:`ApprovalRequest` and returns one of the
        three :data:`ApprovalChoice` outcomes — the display wording lives in the
        env's front-end (the port selector under i18n, or the console fallback).
        Unlike ask_user(), this does NOT treat any reply as a kill switch — an
        approval should never silently deactivate the Role. Without a bound Port
        there is no channel, so it fails closed ("deny") and the engine denies.
        """
        role = self._role
        env = role.human_interaction
        if env is None:
            return ApprovalChoice.reject()
        return await env.request_approval(request, sent_from=role.role_schema.name)

    async def reply_to_user(self, content: str) -> str:
        """Reply to the user with the provided content.

        Requires a bound interaction Port.
        """
        if not content:
            return _MSG_CONTENT_REQUIRED

        role = self._role
        env = role.human_interaction
        if env is None:
            return _MSG_NO_HUMAN_INTERACTION

        return await env.reply_to_user(content, sent_from=role.role_schema.name)

    # ------------------------------------------------------------------
    # Loop coordination
    # ------------------------------------------------------------------

    async def wait_interruptible(self, duration: Optional[float] = None) -> float:
        """Block until an event wakes the agent, optionally bounded by a deadline.

        Wake conditions: a new message arrives in the message buffer (user
        input, background-task notification, etc.), a background task completes,
        or — when *duration* is given — the wall-clock deadline elapses. Owns the
        wait coordination so the Sleep tool stays a thin trigger and never
        touches RoleState, the msg_buffer, or the bg pool.

        When *duration* is a positive number the wait is a **durable timer**: its
        wall-clock deadline is journaled (via the executor's run journal) so a
        crash-resume continues waiting the *remaining* time instead of restarting
        the countdown. A resume adopts a still-in-flight timer's deadline in
        preference to *duration*, and returns at once if it has already passed. A
        ``None`` (or non-positive) *duration* keeps the historical indefinite,
        purely event-driven wait — no timer is journaled.

        Returns:
            slept_seconds — elapsed wait time rounded to 0.1s.
        """
        role = self._role
        msg_buffer = role.state.msg_buffer
        bg_pool = role._peek_bg_pool()

        remaining, step_id = self._durable_timer_setup(duration)

        start = time.time()
        activity = msg_buffer.activity_snapshot()
        waiters: set[asyncio.Task] = {asyncio.create_task(msg_buffer.wait_for_activity(activity.generation))}
        if bg_pool is not None:
            waiters.add(asyncio.create_task(bg_pool.wait_for_completion()))
        if remaining is not None:
            waiters.add(asyncio.create_task(asyncio.sleep(remaining)))

        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in waiters:
                t.cancel()

        if step_id is not None:
            self._durable_timer_complete(step_id)

        return round(time.time() - start, 1)

    def _durable_timer_setup(self, duration: Optional[float]) -> tuple[Optional[float], Optional[str]]:
        """Resolve the wait's remaining time + journal a durable timer if bounded.

        Returns ``(remaining, step_id)``: ``remaining`` is the seconds to wait
        (``None`` = indefinite, event-driven only); ``step_id`` is the journaled
        timer's id to complete afterwards (``None`` when unbounded or unjournaled).

        A resume adopts a still-in-flight timer's deadline (waiting only the time
        left, clamped to 0 if already past) ahead of *duration*; otherwise a
        positive *duration* opens a fresh journaled timer. Journaling is
        best-effort — if the journal is unavailable the wait still runs bounded,
        just without crash-resumability.
        """
        store = self._session_timer_store()
        pending = store.pending()
        if pending:
            timer = min(pending, key=lambda item: item.deadline.epoch_nanoseconds)
            deadline = timer.deadline.to_datetime(expected_clock=timer.deadline.clock)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                store.settle(timer.timer_id, SessionTimerState.MISFIRED)
                return 0.0, None
            return remaining, timer.timer_id
        if duration is None or duration <= 0:
            return None, None
        timer = store.schedule(duration)
        return duration, timer.timer_id

    def _durable_timer_complete(self, step_id: str) -> None:
        """Record the durable timer's terminal (best-effort)."""
        self._session_timer_store().settle(step_id, SessionTimerState.COMPLETED)

    def _session_timer_store(self) -> SessionTimerStore:
        if self._timer_store is None:
            self._timer_store = SessionTimerStore(
                self._role.session_id,
                self._role._components.workspace_store,
            )
        return self._timer_store

    async def end_session(self) -> str:
        """End the current session.

        Deactivates the Role so the run loop terminates. Successful output flows
        through ``RunResult``; this tool return is only an execution acknowledgment.
        """
        self._role.deactivate()
        return ""
