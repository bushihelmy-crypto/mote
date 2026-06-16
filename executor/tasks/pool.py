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
import traceback
from typing import TYPE_CHECKING, Coroutine, Literal, Optional
from xml.sax.saxutils import escape as _escape_xml

from metagpt.common.logs import log_class
from metagpt.common.schema import CauseBy, MessagePriority, MessageQueue
from metagpt.executor.tasks.types import BackgroundTaskNotification, BgStatus, TaskMeta, TaskType

if TYPE_CHECKING:
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
        msg_buffer: MessageQueue,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        output_store: "Optional[TaskOutputStore]" = None,
    ) -> None:
        self._msg_buffer = msg_buffer
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
        coro: Coroutine,
        command_name: str,
        timeout: Optional[float] = _DEFAULT_TASK_TIMEOUT,
        task_type: str = TaskType.COROUTINE,
        task_kind: Optional[str] = None,
        agent_id: Optional[str] = None,
        progress: bool = False,
        graph_ref=None,
        initial_params: Optional[dict] = None,
        factory=None,
        max_restarts: int = 3,
    ) -> str:
        """Submit a coroutine for background execution.

        Args:
            coro: The coroutine to run.
            command_name: Human-readable name for logging/notifications.
            timeout: Per-task timeout in seconds. ``None`` disables the
                timeout. Defaults to 600s (10 min).
            task_type: Task type classification (shell/coroutine/agent).
            task_kind: Optional sub-type (e.g. "bash", "monitor").
            agent_id: Optional owning agent identifier.
            progress: When *True* and an ``output_store`` is configured, install
                a per-task progress sink so ``bggraph`` node events
                (``report_progress``) are appended to the task's disk output.
            graph_ref: Optional BgGraph reference for per-node resume/skip.
            initial_params: Original kwargs the task was created with (restart).
            factory: Rebuild factory for restarting from scratch.
            max_restarts: Maximum number of allowed restarts for this task.

        Returns a task_id like ``bg_1``, ``bg_2``, etc.
        """
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
        meta.graph_ref = graph_ref
        meta.initial_params = initial_params
        meta.factory = factory
        meta.max_restarts = max_restarts
        self._meta[task_id] = meta

        # Progress sink (innermost wrapper): set the bggraph progress writer in
        # the running task's context before the driver coroutine starts, so node
        # events and the terminal notification land on disk.
        if progress and self._output_store is not None:
            self._output_store.init_output(task_id)
            meta.output_path = self._output_store.get_output_path(task_id)
            coro = self._with_progress(coro, task_id)

        if timeout is not None and timeout > 0:
            coro = self._with_timeout(coro, timeout)

        coro = self._run_with_semaphore(coro, task_id)

        task = asyncio.create_task(coro)
        self._tasks[task_id] = task
        task.add_done_callback(lambda t: self._on_done(task_id, command_name, t))
        return task_id

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
        coro: Coroutine,
        *,
        timeout: Optional[float] = None,
        progress: bool = True,
    ) -> str:
        """Re-submit a coroutine under an existing task_id (for resume/retry).

        Resets the task's status to RUNNING and attaches a fresh asyncio.Task.
        The existing ``TaskMeta`` (graph_ref, initial_params, etc.) is preserved.

        Args:
            task_id: An existing task_id previously returned by :meth:`submit`.
            coro: The new coroutine to run (e.g. a resumed graph driver).
            timeout: Optional per-task timeout in seconds.
            progress: When *True* and an output_store is configured, wrap the
                coro with a progress sink (appends to the existing output file).

        Returns:
            The same *task_id* for convenience.

        Raises:
            ValueError: If *task_id* is not known to this pool.
        """
        meta = self._meta.get(task_id)
        if meta is None:
            raise ValueError(f"Unknown task_id: {task_id}")

        meta.status = BgStatus.RUNNING
        meta.start_time = time.time()
        meta.end_time = None
        meta.result = None
        meta.notified = False
        meta.retry_count += 1

        if progress and self._output_store is not None:
            # Output already initialized from original submit; just wrap progress.
            coro = self._with_progress(coro, task_id)
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

    async def _with_progress(self, coro: Coroutine, task_id: str):
        """Run *coro* with the bggraph progress writer bound to this task.

        The writer renders each ``report_progress`` event and appends it to the
        task's disk output. The contextvar is set inside the running task so it
        propagates to the driver coroutine and the node tasks it spawns.
        """
        from metagpt.executor.tasks.bggraph.report import (
            make_progress_writer,
            reset_progress_writer,
            set_progress_writer,
        )

        store = self._output_store
        writer = make_progress_writer(lambda line: store.append(task_id, line), task_id=task_id)
        token = set_progress_writer(writer)
        try:
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

    def _on_done(self, task_id: str, command_name: str, task: asyncio.Task) -> None:
        """Synchronous callback invoked by the event loop when a task finishes."""
        from metagpt.executor.tasks.bggraph.types import LlmPauseResult

        status: str
        result: Optional[str] = None
        summary: str

        if task.cancelled():
            status = BgStatus.CANCELLED
            meta = self._meta.get(task_id)
            if meta is not None and meta._output_capped:
                result = f"Background command killed: output exceeded {_OUTPUT_CAP_DISPLAY} disk cap."
                summary = f"{command_name} was killed because its output exceeded the disk size limit."
            else:
                summary = f"{command_name} was cancelled."
        else:
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, asyncio.TimeoutError):
                    status = BgStatus.TIMEOUT
                    summary = f"{command_name} timed out after exceeding the time limit."
                else:
                    status = BgStatus.FAILED
                    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    result = tb[:_MAX_RESULT_LEN]
                    summary = f"{command_name} failed."
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
            meta.notified = True

        body = self._build_xml(task_id, command_name, status, summary, result=result)

        # Push structured notification into the agent's message buffer.
        notification = BackgroundTaskNotification(
            content=body,
            cause_by=CauseBy.RUN_COMMAND,
            task_id=task_id,
            command_name=command_name,
            status=status,
            result=result,
        )
        self._msg_buffer.push(notification, priority=MessagePriority.NEXT)

        # Resolve every registered one-shot completion future (fan-out
        # broadcast). Iterate a snapshot so a re-entrant completion is safe;
        # each resolved/cancelled future removes itself via _discard_waiter.
        for fut in list(self._completion_waiters):
            if not fut.done():
                fut.set_result(None)

        # Remove from tracking dict
        self._tasks.pop(task_id, None)
