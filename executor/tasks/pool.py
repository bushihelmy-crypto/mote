"""Background task pool for running slow commands asynchronously.

Manages asyncio.Task lifecycle and pushes completion notifications
into the agent's msg_buffer so the existing _observe() loop picks
them up automatically.

Notification design (aligned with Claude Code's ``<task-notification>``):
    Each completion pushes a ``BackgroundTaskNotification`` (subclass of
    ``UserMessage``) that carries both a human-readable ``content`` string
    **and** machine-readable structured fields (``task_id``, ``command_name``,
    ``status``, ``result``).  Downstream code can use ``is_bg_notification()``
    to filter these without parsing free text.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Literal, Optional
from xml.sax.saxutils import escape as _escape_xml

from metagpt.common.logs import log_class
from metagpt.common.schema import CauseBy, MessagePriority
from metagpt.executor.tasks.types import (
    BackgroundTaskNotification,
    BgStatus,
    GraphMeta,
    PollFactory,
    TaskMeta,
    TaskType,
)

if TYPE_CHECKING:
    from metagpt.common.interface import MessageSink
    from metagpt.executor.tasks.disk_output import TaskOutputStore
from metagpt.common.const.tasks import (
    MAX_RESULT_LEN as _MAX_RESULT_LEN,
    DEFAULT_TASK_TIMEOUT as _DEFAULT_TASK_TIMEOUT,
    DEFAULT_WAIT_COMPLETION_TIMEOUT as _DEFAULT_WAIT_COMPLETION_TIMEOUT,
    DEFAULT_MAX_CONCURRENCY as _DEFAULT_MAX_CONCURRENCY,
    MAX_TASK_OUTPUT_BYTES_DISPLAY as _OUTPUT_CAP_DISPLAY,
)


@log_class(
    level="DEBUG",
    # Hot/trivial state queries polled by the loop — tracing them only adds noise.
    exclude={"has_pending", "pending_count", "pending_ids", "get_task_info", "list_tasks", "list_tasks_for_agent"},
)
class BackgroundTaskPool:
    """Pool of background asyncio tasks with event-driven completion notification."""

    def __init__(
        self,
        msg_buffer: "MessageSink",
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        output_store: "Optional[TaskOutputStore]" = None,
        wake: Optional[Callable[[], None]] = None,
    ) -> None:
        self._msg_buffer = msg_buffer
        # Runtime wake callback. ``msg_buffer.push`` wakes mid-turn waiters
        # (its built-in new-message signal), but the scheduler driver parks
        # *between* turns on a separate ``wake_event``; pushing alone won't
        # restart it. So a completion both pushes the notification (delivery)
        # and calls ``wake`` (restart a parked driver). Optional / late-bound
        # via :meth:`set_wake` because the runtime wiring may arrive after the
        # pool is constructed.
        self._wake = wake
        # Optional disk-output store. When present, graph tasks submitted with
        # ``progress=True`` get a per-task output sink so node-level
        # ``report_progress`` events land on disk and surface as
        # ``<delta-summary>`` blocks via ``TaskAttachmentGenerator``.
        self._output_store = output_store
        self._tasks: dict[str, asyncio.Task] = {}
        self._meta: dict[str, TaskMeta] = {}  # all tasks (running + completed)
        self._counter = 0
        # One-shot completion futures. Each waiter (wait_any / wait_all /
        # wait_for_completion) registers its own future via _next_completion;
        # _on_done resolves them all on the next task completion. There is no
        # shared level-triggered flag, so nothing to clear — and therefore no
        # clear-then-wait ordering to get wrong (lost-wakeup-proof by design).
        self._completion_waiters: list[asyncio.Future] = []
        self._semaphore = asyncio.Semaphore(max_concurrency)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        poll_factory: "PollFactory",
        command_name: str,
        timeout: Optional[float] = _DEFAULT_TASK_TIMEOUT,
        task_type: str = TaskType.COROUTINE,
        task_kind: Optional[str] = None,
        agent_id: Optional[str] = None,
        progress: bool = False,
        graph_meta: "Optional[GraphMeta]" = None,
        max_restarts: int = 3,
    ) -> str:
        """Submit a poll factory for background execution.

        Args:
            poll_factory: A :data:`PollFactory` callable that returns a
                coroutine. The factory is invoked immediately to produce the
                coroutine that will be scheduled as an asyncio.Task.
            command_name: Human-readable name for logging/notifications.
            timeout: Per-task timeout in seconds. ``None`` disables the
                timeout. Defaults to 600s (10 min).
            task_type: Task type classification (shell/coroutine/agent).
            task_kind: Optional sub-type (e.g. "bash", "monitor").
            agent_id: Optional owning agent identifier.
            progress: When *True* and an ``output_store`` is configured, install
                a per-task progress sink so ``bggraph`` node events
                (``report_progress``) are appended to the task's disk output.
            graph_meta: Optional :class:`GraphMeta` bundle for graph resume.
            max_restarts: Maximum number of allowed restarts for this task.

        Returns a task_id like ``bg_1``, ``bg_2``, etc.
        """
        coro = poll_factory()
        self._counter += 1
        task_id = f"bg_{self._counter}"

        meta = TaskMeta(
            task_id=task_id,
            command_name=command_name,
            status=BgStatus.PENDING,
            task_type=task_type,
            task_kind=task_kind,
            agent_id=agent_id,
        )
        meta.graph_meta = graph_meta
        if graph_meta is not None and graph_meta.run_state is not None:
            # Snapshot the authoritative run_state the driver mutates, so resume
            # and GetNodeState read true node status (not value-inference).
            meta.run_state = graph_meta.run_state
        meta.max_restarts = max_restarts
        self._meta[task_id] = meta

        # Progress sink (innermost wrapper): set the bggraph progress writer in
        # the running task's context before the driver coroutine starts, so node
        # events and the terminal notification land on disk.
        if progress and self._output_store is not None:
            self._output_store.init_output(task_id)
            meta.output_path = self._output_store.get_output_path(task_id)
            # Capture the spawner's live bus *now* (synchronously, before the
            # task is created) and hand it to the wrapper explicitly, rather than
            # leaning on the contextvar surviving the create_task boundary.
            from metagpt.common.events import current_bus

            coro = self._with_progress(coro, task_id, current_bus())

        if timeout is not None and timeout > 0:
            coro = self._with_timeout(coro, timeout)

        coro = self._run_with_semaphore(coro, task_id)

        task = asyncio.create_task(coro)
        self._tasks[task_id] = task
        task.add_done_callback(lambda t: self._on_done(task_id, command_name, t))
        return task_id

    def set_wake(self, wake: Optional[Callable[[], None]]) -> None:
        """Bind (or rebind) the runtime wake callback.

        Late-bindable because the runtime/scheduler wiring may be installed
        after this pool is constructed.
        """
        self._wake = wake

    def has_pending(self) -> bool:
        """Return *True* if there are still running tasks."""
        return bool(self._tasks)

    async def wait_for_completion(
        self, timeout: Optional[float] = _DEFAULT_WAIT_COMPLETION_TIMEOUT
    ) -> bool:
        """Block until the next background task completes, or *timeout* elapses.

        Registers a one-shot completion future and awaits it; the next task to
        finish resolves it. Exposed so collaborators (``Role.wait_interruptible``)
        can await completion without touching pool internals.

        Note: waits for the *next* completion unconditionally — it does NOT
        self-check ``has_pending()``. The ``timeout`` (default 10 min) is a
        safety bound so a bare call on an idle/empty pool returns instead of
        blocking forever; pass ``None`` to wait without a bound. Callers that
        already bound the wait themselves (e.g. ``wait_interruptible`` races it
        against a sleep) can ignore it.

        Returns:
            ``True`` if a task completed, ``False`` if the timeout elapsed first.
        """
        fut = self._next_completion()
        if timeout is None:
            await fut
            return True
        try:
            await asyncio.wait_for(fut, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # wait_for cancelled the future; _discard_waiter has dropped it.
            return False

    # Wake-up reason type returned by ``wait_any``.
    WakeReason = Literal["task_done", "new_message", "timeout"]

    async def wait_any(self, timeout: float = 120.0) -> WakeReason:
        """Block until a background task finishes, a new message arrives in
        the msg_buffer, or *timeout* expires.

        Returns:
            ``"task_done"``    — at least one background task completed.
            ``"new_message"``  — msg_buffer received a new message (user input,
                                 external event, etc.).
            ``"timeout"``      — neither happened within the time limit.
        """
        done_waiter = self._next_completion()
        msg_waiter = asyncio.create_task(self._msg_buffer.wait_for_message())

        try:
            done, _pending = await asyncio.wait(
                {done_waiter, msg_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            done_waiter.cancel()
            msg_waiter.cancel()

        if not done:
            return "timeout"
        if done_waiter in done:
            return "task_done"
        return "new_message"

    @property
    def pending_count(self) -> int:
        """Number of tasks still running."""
        return len(self._tasks)

    @property
    def pending_ids(self) -> list[str]:
        """Task ids of all running tasks (snapshot)."""
        return list(self._tasks.keys())

    def get_task_info(self, task_id: str) -> Optional[TaskMeta]:
        """Return metadata for a task (running or recently completed).

        Returns ``None`` if the task_id is unknown.
        """
        return self._meta.get(task_id)

    def get_run_state(self, task_id: str) -> Optional[Any]:
        """Return the authoritative :class:`GraphRunState` for a graph task.

        The truth source for per-node status / attempts / failure reason, read by
        :class:`GetNodeState` and resume. ``None`` if the task is unknown or is
        not a graph task (no run_state recorded).
        """
        meta = self._meta.get(task_id)
        return meta.run_state if meta is not None else None

    def list_tasks(self) -> list[TaskMeta]:
        """Return metadata for all tracked tasks (running + recently completed)."""
        return list(self._meta.values())

    def cancel(self, task_id: str) -> bool:
        """Cancel a running background task.

        Returns ``True`` if the task was found and cancel was requested,
        ``False`` if the task_id is unknown or already finished.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True

    def cancel_for_cap(self, task_id: str) -> bool:
        """Cancel a task because its output exceeded the disk size cap.

        Sets a flag so ``_on_done`` includes the cap reason in ``result``,
        then delegates to :meth:`cancel`.  Aligned with Claude Code's
        ``#killedForSize`` flag in ``ShellCommand.ts``.
        """
        meta = self._meta.get(task_id)
        if meta is not None:
            meta._output_capped = True
        return self.cancel(task_id)

    def list_tasks_for_agent(self, agent_id: str) -> list[TaskMeta]:
        """Return all tasks belonging to *agent_id*."""
        return [m for m in self._meta.values() if m.agent_id == agent_id]

    def cancel_tasks_for_agent(self, agent_id: str) -> list[str]:
        """Cancel all running tasks belonging to *agent_id*.

        Returns the list of task_ids that were cancelled.
        """
        cancelled: list[str] = []
        for meta in self._meta.values():
            if meta.agent_id != agent_id:
                continue
            if meta.status in (BgStatus.PENDING, BgStatus.RUNNING):
                if self.cancel(meta.task_id):
                    cancelled.append(meta.task_id)
        return cancelled

    def resubmit(
        self,
        task_id: str,
        poll_factory: "PollFactory",
        *,
        timeout: Optional[float] = None,
        progress: bool = True,
        graph_meta: "Optional[GraphMeta]" = None,
    ) -> str:
        """Re-submit a poll factory under an existing task_id (for resume/retry).

        Resets the task's status to RUNNING and attaches a fresh asyncio.Task.
        The existing ``TaskMeta`` (graph_meta, etc.) is preserved.

        Args:
            task_id: An existing task_id previously returned by :meth:`submit`.
            poll_factory: A :data:`PollFactory` callable that returns a coroutine.
            timeout: Optional per-task timeout in seconds.
            progress: When *True* and an output_store is configured, wrap the
                coro with a progress sink (appends to the existing output file).
            graph_meta: Optional fresh :class:`GraphMeta` from a resume builder.
                When provided it replaces the stored one and re-snapshots its
                ``run_state`` so the new driver mutates the meta's tracked object.

        Returns:
            The same *task_id* for convenience.

        Raises:
            ValueError: If *task_id* is not known to this pool.
        """
        meta = self._meta.get(task_id)
        if meta is None:
            raise ValueError(f"Unknown task_id: {task_id}")

        if meta.retry_count >= meta.max_restarts:
            raise ValueError(
                f"Task {task_id} reached restart limit ({meta.retry_count}/{meta.max_restarts})"
            )

        if graph_meta is not None:
            meta.graph_meta = graph_meta
            if graph_meta.run_state is not None:
                meta.run_state = graph_meta.run_state

        meta.status = BgStatus.RUNNING
        meta.start_time = time.time()
        meta.end_time = None
        meta.result = None
        meta.notified = False
        meta.retry_count += 1

        coro = poll_factory()

        if progress and self._output_store is not None:
            # Output already initialized from original submit; just wrap progress.
            from metagpt.common.events import current_bus

            coro = self._with_progress(coro, task_id, current_bus())
        if timeout is not None and timeout > 0:
            coro = self._with_timeout(coro, timeout)
        coro = self._run_with_semaphore(coro, task_id)

        task = asyncio.create_task(coro)
        self._tasks[task_id] = task
        task.add_done_callback(lambda t: self._on_done(task_id, meta.command_name, t))
        return task_id

    def adopt(
        self,
        task: asyncio.Task,
        command_name: str,
        task_type: str = TaskType.COROUTINE,
        task_kind: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> str:
        """Adopt an already-running ``asyncio.Task`` into the pool.

        Unlike :meth:`submit`, this does **not** wrap the task with a
        semaphore or timeout — the task is already executing.  The pool
        will track it, update :class:`TaskMeta` on completion, and push
        a ``BackgroundTaskNotification`` as usual.

        Args:
            task: A running ``asyncio.Task`` to track.
            command_name: Human-readable label for notifications.
            task_type: Task type classification (shell/coroutine/agent).
            task_kind: Optional sub-type (e.g. "bash", "monitor").
            agent_id: Optional owning agent identifier.

        Returns:
            A new task_id (e.g. ``"bg_3"``).
        """
        self._counter += 1
        task_id = f"bg_{self._counter}"

        meta = TaskMeta(
            task_id=task_id,
            command_name=command_name,
            status=BgStatus.RUNNING,
            task_type=task_type,
            task_kind=task_kind,
            agent_id=agent_id,
        )
        self._meta[task_id] = meta
        self._tasks[task_id] = task
        task.add_done_callback(lambda t: self._on_done(task_id, command_name, t))
        return task_id

    async def wait_all(self) -> None:
        """Block until every pending task has finished (or timed out)."""
        while self.has_pending():
            await self._next_completion()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_completion(self) -> asyncio.Future:
        """Return a one-shot future resolved on the NEXT task completion.

        Each waiter gets its own future, so there is no shared level-triggered
        flag to clear (hence none to clear in the wrong order). Any completion
        after the future is created resolves it exactly once. The future drops
        itself from the registry when done — resolved or cancelled — so an
        abandoned waiter (e.g. a timed-out ``wait_any``) never leaks.
        """
        fut = asyncio.get_running_loop().create_future()
        self._completion_waiters.append(fut)
        fut.add_done_callback(self._discard_waiter)
        return fut

    def _discard_waiter(self, fut: asyncio.Future) -> None:
        """Drop a completed/cancelled completion future from the registry."""
        try:
            self._completion_waiters.remove(fut)
        except ValueError:
            pass

    async def _run_with_semaphore(self, coro: Coroutine, task_id: str):
        """Acquire the concurrency semaphore before running *coro*."""
        async with self._semaphore:
            meta = self._meta.get(task_id)
            if meta is not None and meta.status == BgStatus.PENDING:
                meta.status = BgStatus.RUNNING
                meta.start_time = time.time()
            return await coro

    @staticmethod
    async def _with_timeout(coro: Coroutine, timeout: float):
        """Wrap *coro* so it raises ``asyncio.TimeoutError`` after *timeout* seconds."""
        return await asyncio.wait_for(coro, timeout=timeout)

    async def _with_progress(self, coro: Coroutine, task_id: str, bus=None):
        """Run *coro* with the bggraph progress writer bound to this task.

        The writer renders each ``report_progress`` event and appends it to the
        task's disk output. The contextvar is set inside the running task so it
        propagates to the driver coroutine and the node tasks it spawns.

        ``bus`` is the event bus captured *synchronously at spawn time* (the only
        moment the spawner's live contextvar is guaranteed visible). It is
        re-bound here, inside the spawned task, with an explicit ``set_bus`` —
        so progress telemetry (``_emit_task_progress``) reaches the right bus by
        an explicit hand-off, not by relying on ``create_task`` snapshotting the
        contextvar across the spawn boundary. Pure observation, so losing it
        could only ever drop a progress mirror, never a control veto.
        """
        from metagpt.common.events import set_bus
        from metagpt.executor.tasks.bggraph.report import (
            make_progress_writer,
            reset_progress_writer,
            set_progress_writer,
        )

        store = self._output_store
        meta = self._meta.get(task_id)
        command_name = meta.command_name if meta is not None else ""
        writer = make_progress_writer(
            lambda line: store.append(task_id, line),
            task_id=task_id,
            command_name=command_name,
            deliver=self.deliver,
        )
        token = set_progress_writer(writer)
        try:
            with set_bus(bus):
                return await coro
        finally:
            reset_progress_writer(token)

    @staticmethod
    def _build_xml(task_id: str, command_name: str, status: str, summary: str, result: Optional[str] = None) -> str:
        """Build a ``<task-notification>`` XML envelope aligned with Claude Code."""
        lines = [
            "<task-notification>",
            f"<task-id>{task_id}</task-id>",
            f"<command>{_escape_xml(command_name)}</command>",
            f"<status>{status}</status>",
            f"<summary>{_escape_xml(summary)}</summary>",
        ]
        if result is not None:
            lines.append(f"<result>{_escape_xml(result)}</result>")
        lines.append("</task-notification>")
        return "\n".join(lines)

    def _wake_runtime(self) -> None:
        """Restart a scheduler driver parked between turns (best-effort).

        ``msg_buffer.push`` already wakes any *mid-turn* waiter via its
        new-message signal; this additionally restarts a driver parked on the
        runtime's separate ``wake_event``. A wake failure must never break the
        completion path.
        """
        if self._wake is None:
            return
        try:
            self._wake()
        except Exception:  # noqa: BLE001 — best-effort wake
            pass

    def deliver(self, notification: BackgroundTaskNotification) -> None:
        """Single delivery choke point for a background-task notification.

        Pushes *notification* into the agent's inbox at ``NEXT`` priority (so
        the react loop observes it next turn) and wakes a parked scheduler
        driver. Owned by the pool because it holds both the ``msg_buffer``
        reference and the ``wake`` callback — the producers that emit
        notifications (``_on_done`` for the one whole-task terminal, the bggraph
        progress writer for node-level / START events, and the stall detector)
        all route through here, so push+wake lives in exactly one place.

        Stateless: there is no longer a one-terminal-per-task guard because there
        is exactly one terminal producer. ``_on_done`` fires once per task and is
        the *sole* emitter of a ``task_terminal`` notification (the in-graph
        writer no longer delivers terminals — it only records the rich DAG
        snapshot to disk and pushes non-terminal node/START events). So no two
        producers can race the same task's terminal. Best-effort: a delivery or
        wake failure must never break the pipeline.
        """
        try:
            self._msg_buffer.push(notification, priority=MessagePriority.NEXT)
        except Exception:  # noqa: BLE001 — delivery must never break the pipeline
            return
        self._wake_runtime()

    def _on_done(self, task_id: str, command_name: str, task: asyncio.Task) -> None:
        """Synchronous callback invoked by the event loop when a task finishes.

        Pushes the structured completion notification directly into the
        agent's msg_buffer (NEXT priority) and wakes the runtime so a parked
        scheduler driver starts a new react turn. No event-bus round-trip:
        completion is a pure observation with a single consumer, so it goes
        straight to the queue the react loop observes.
        """
        from metagpt.common.exception import (
            BackgroundTaskCancelledError,
            BackgroundTaskTimeoutError,
            ErrorReport,
            render_error_block,
        )
        from metagpt.executor.tasks.bggraph.types import LlmPauseResult

        status: str
        result: Optional[str] = None
        summary: str
        error_dict: Optional[dict] = None

        if task.cancelled():
            status = BgStatus.CANCELLED
            meta = self._meta.get(task_id)
            if meta is not None and meta._output_capped:
                summary = f"{command_name} was killed because its output exceeded the disk size limit."
                cancel_msg = (
                    f"Background command killed: output exceeded {_OUTPUT_CAP_DISPLAY} disk cap."
                )
            else:
                summary = f"{command_name} was cancelled."
                cancel_msg = summary
            # Synthesize a typed error so a cancellation surfaces the same
            # structured <error> block as every other terminal outcome.
            report = ErrorReport.from_exception(BackgroundTaskCancelledError(cancel_msg))
            error_dict = report.as_dict()
            result = render_error_block(report)[:_MAX_RESULT_LEN]
        else:
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, asyncio.TimeoutError):
                    status = BgStatus.TIMEOUT
                    summary = f"{command_name} timed out after exceeding the time limit."
                    # Route timeout through the shared contract too (it was a
                    # bypass before — error_dict stayed None), so the model gets
                    # the uniform block + machine-readable report.
                    report = ErrorReport.from_exception(BackgroundTaskTimeoutError(summary))
                    error_dict = report.as_dict()
                    result = render_error_block(report)[:_MAX_RESULT_LEN]
                    # Snapshot graph state on timeout so the task can be resumed
                    # from where it stalled. Unlike a driver-raised failure, the
                    # bare asyncio.TimeoutError is raised by wait_for *outside*
                    # the driver and carries no state — so read the live objects
                    # the driver mutates off graph_meta (run_state is already
                    # tracked from submit; state_snapshot is the missing piece
                    # that otherwise forces resume into a full restart).
                    meta = self._meta.get(task_id)
                    if meta is not None and meta.graph_meta is not None:
                        gm = meta.graph_meta
                        if gm.run_state is not None:
                            meta.run_state = gm.run_state
                            meta.completed_nodes = gm.run_state.completed_names()
                        if gm.state is not None:
                            meta.state_snapshot = gm.state
                else:
                    status = BgStatus.FAILED
                    # Normalize through the shared error contract instead of
                    # dumping a raw traceback: the model gets a uniform <error>
                    # block (code/recovery/structured detail), and the machine-
                    # readable report rides along on the notification's `error`.
                    report = ErrorReport.from_exception(exc)
                    error_dict = report.as_dict()
                    result = render_error_block(report)[:_MAX_RESULT_LEN]
                    summary = f"{command_name} failed."
                    # Snapshot state on failure (not only on LLM pause) so the
                    # graph can be resumed from where it broke. The driver
                    # attaches these to the exception before raising.
                    meta = self._meta.get(task_id)
                    if meta is not None:
                        run_state = getattr(exc, "run_state", None)
                        if run_state is not None:
                            meta.run_state = run_state
                            meta.completed_nodes = run_state.completed_names()
                        graph_state = getattr(exc, "graph_state", None)
                        if graph_state is not None:
                            meta.state_snapshot = graph_state
            else:
                raw = task.result()
                if isinstance(raw, LlmPauseResult):
                    status = BgStatus.WAITING_FOR_ROUTE
                    summary = f"{command_name} paused, waiting for route selection."
                    # Save pause state to meta for resume.
                    meta = self._meta.get(task_id)
                    if meta is not None:
                        meta.state_snapshot = raw.state
                        meta.completed_nodes = raw.completed
                        if raw.run_state is not None:
                            meta.run_state = raw.run_state
                else:
                    status = BgStatus.SUCCESS
                    result_str = str(raw) if raw is not None else "(no output)"
                    if len(result_str) > _MAX_RESULT_LEN:
                        result_str = result_str[:_MAX_RESULT_LEN] + "...(truncated)"
                    result = result_str
                    summary = f"{command_name} completed successfully."

        # Update task metadata.
        meta = self._meta.get(task_id)
        if meta is not None:
            meta.status = status
            meta.end_time = time.time()
            meta.result = result
            meta.error = error_dict
            meta.notified = True

        body = self._build_xml(task_id, command_name, status, summary, result=result)

        # Build the structured notification. This is always a whole-task
        # terminal (``_on_done`` fires exactly once, when the task ends).
        notification = BackgroundTaskNotification(
            content=body,
            cause_by=CauseBy.RUN_COMMAND,
            task_id=task_id,
            command_name=command_name,
            status=status,
            result=result,
            error=error_dict,
            task_terminal=True,
        )

        # Deliver via the single choke point. ``_on_done`` is the sole producer
        # of a task's whole-task terminal (the in-graph writer only records the
        # rich DAG snapshot to disk and pushes non-terminal node/START events),
        # so this terminal always gets through — including the interruption case
        # (timeout / external cancel) where the coroutine never reached its own
        # terminal code. The agent sees exactly one terminal, no dedup needed.
        self.deliver(notification)

        # Resolve every registered one-shot completion future (fan-out
        # broadcast). Iterate a snapshot so a re-entrant completion is safe;
        # each resolved/cancelled future removes itself via _discard_waiter.
        for fut in list(self._completion_waiters):
            if not fut.done():
                fut.set_result(None)

        # Remove from tracking dict
        self._tasks.pop(task_id, None)
