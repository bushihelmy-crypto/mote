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
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from mote.common.agent_control import ContextPolicy, Lifecycle, SpawnContext, SpawnSpec, spawn_and_run
from mote.common.logs import logger
from mote.common.schema import UserMessage
from mote.common.text.hashing import content_hash
from mote.roles.role_state import RoleState
from mote.session.hunk_ledger import EXTERNAL

if TYPE_CHECKING:
    from mote.common.schema.permission_types import ApprovalChoice, ApprovalRequest
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

    def record_file_baseline(self, full_path: str) -> None:
        """Acknowledge a file's current on-disk content as mote's known baseline.

        Called after every agent Write/Edit (via the file tools'
        ``_refresh_read_state``): the just-written content becomes the baseline
        the read-before-write guard diffs against when it later detects an
        *external* modification, so the external delta is attributed cleanly
        without mis-crediting the agent's own edits. The content is blobbed into
        the session's content-addressed store under its digest (so the guard can
        fetch it back) and the digest is recorded in the Role's file-baseline
        state. No-op when hunk recording is disabled; best-effort (never raises).
        """
        if not self._role.role_schema.record_hunks:
            return
        try:
            content = Path(full_path).read_bytes()
        except OSError:
            return  # missing/unreadable (e.g. a delete) — nothing to baseline
        try:
            digest = self._role.file_snapshot_recorder.blobs.put(content)
            self._role._state_ctl.record_file_baseline(full_path, digest)
        except Exception as exc:  # noqa: BLE001 — baseline capture must not break a write
            logger.warning(f"record_file_baseline: failed for '{full_path}': {exc}")

    def attribute_external_change(self, full_path: str) -> None:
        """Attribute an out-of-band file modification as ``external`` hunks.

        Called by the read-before-write guard the instant it detects that a file
        changed on disk since mote last knew it (mtime mismatch), *before* the
        guard aborts the write. Diffs the current on-disk content against the
        recorded baseline (the content mote last wrote/knew; empty when mote only
        ever read the file, so the whole current content reads as external), and
        hands the delta to :meth:`HunkLedger.record_delta`, which splits it into
        ``source=external`` hunks — stamped with the live turn index and no
        tool-call id (there is no agent tool call). Then re-baselines to the
        current content so mote acknowledges what it now sees.

        No-op when hunk recording is disabled or the file is unreadable;
        best-effort (never raises into the guard).
        """
        if not self._role.role_schema.record_hunks:
            return
        ledger = self._role.hunk_ledger
        if ledger is None:
            return
        try:
            current = Path(full_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        blobs = self._role.file_snapshot_recorder.blobs
        baseline_digest = self._role._state_ctl.get_file_baseline(full_path)
        baseline = ""
        if baseline_digest:
            blob = blobs.get(baseline_digest)
            if blob is not None:
                baseline = blob.decode("utf-8", errors="replace")
        # A stable, content-derived id base: an external edit has no tool-call id,
        # and re-detecting the same unchanged external state must fold onto the
        # same records rather than duplicate them.
        base = content_hash(f"external|{full_path}|{content_hash(baseline)}|{content_hash(current)}")
        ledger.record_delta(
            blobs,
            path=full_path,
            old=baseline,
            new=current,
            source=EXTERNAL,
            turn_index=self._role.current_turn_index(),
            id_base=base,
        )
        # Acknowledge the now-seen content as the baseline going forward (also
        # preserves it in the blob store), so a follow-up guard fire before any
        # further change is a content-identical no-op.
        self.record_file_baseline(full_path)

    def register_resource(self, *, id: str, kind: str, content: str) -> None:
        """Register a loaded capability body for post-compaction re-projection.

        Delegates to the Role's ``resource_registry``. The Skill tool calls this
        after rendering an inline skill body so the body is re-projected after an
        autocompaction discards the head. Best-effort — the registry's ``load``
        is a plain dict write and does not raise.
        """
        self._role.resource_registry.load(id=id, kind=kind, content=content)

    def register_task_result(self, task_id: str, content: str) -> None:
        """Register a background task's push-once result for re-projection.

        The pool's terminal callback calls this so a graph terminal / agent
        summary / pause marker survives an autocompaction that discards the live
        ``BackgroundTaskNotification`` — the registry re-projects the ``content``
        pointer right after the summary until the model consumes (and unloads)
        it or the round cap recycles it. Best-effort dict write; does not raise.
        """
        self._role.resource_registry.load(id=task_id, kind="task_result", content=content, sticky=True)

    def retire_task_result(self, task_id: str) -> None:
        """Stop re-projecting a task result once the model has consumed it.

        Called when a consume tool (GetNodeState / resume / cancel) reports the
        result was acted on: unload from the registry so future projections skip
        it. Idempotent — unloading an absent id is a no-op.
        """
        self._role.resource_registry.unload(task_id)

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

    async def request_approval(self, request: "ApprovalRequest") -> "ApprovalChoice":
        """Ask the human to approve a gated tool call; return their decision.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Takes a language-neutral :class:`ApprovalRequest` and returns one of the
        three :data:`ApprovalChoice` outcomes — the display wording lives in the
        env's front-end (the port selector under i18n, or the console fallback).
        Unlike ask_user(), this does NOT treat any reply as a kill switch — an
        approval should never silently deactivate the Role. Outside a MoteEnv
        there is no channel, so it fails closed ("deny") and the engine denies.
        """
        role = self._role
        env = role.state.env
        if env is None:
            return "deny"
        return await env.request_approval(request, sent_from=role.role_schema.name)

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
        waiters: set[asyncio.Task] = {asyncio.create_task(msg_buffer.wait_for_message())}
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
        from mote.loop.durable import begin_timer, resume_timer

        journal = self._run_journal()
        if journal is not None:
            resumed = resume_timer(journal)
            if resumed is not None:
                step_id, deadline = resumed
                return max(0.0, deadline - time.time()), step_id
        if duration is None or duration <= 0:
            return None, None
        if journal is None:
            return duration, None
        step_id, _deadline = begin_timer(journal, duration)
        return duration, step_id

    def _durable_timer_complete(self, step_id: str) -> None:
        """Record the durable timer's terminal (best-effort)."""
        from mote.loop.durable import complete_timer

        journal = self._run_journal()
        if journal is not None:
            complete_timer(journal, step_id)

    def _run_journal(self):
        """The executor's shared run journal, or ``None`` when durability is off."""
        try:
            return self._role.executor.journal
        except Exception:
            return None

    async def end_session(self) -> str:
        """End the current session.

        Deactivates the Role so the run loop terminates. The terminal reply is
        captured into ``state.last_end_output`` by the run loop's post-loop
        finalization (see Role.run), which is the single channel an ephemeral
        spawn's read-back reads — so returning "" here is fine.
        """
        self._role.deactivate()
        return ""
