"""Background task pool for running slow commands asynchronously.

Manages asyncio.Task lifecycle and pushes completion notifications
into the agent's msg_buffer so the existing _observe() loop picks
them up automatically.

Notification design (built around a ``<task-notification>`` envelope):
    Each completion pushes a ``BackgroundTaskNotification`` (subclass of
    ``UserMessage``) that carries both a human-readable ``content`` string
    **and** machine-readable structured fields (``task_id``, ``command_name``,
    ``status``, ``result``).  Downstream code can use ``is_bg_notification()``
    to filter these without parsing free text.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Callable, Coroutine, Literal, Optional, Protocol
from xml.sax.saxutils import escape as _escape_xml

from mote.contracts.config.tool import ToolResultLimitConfig
from mote.contracts.conversation import CauseBy, MessagePriority
from mote.orchestration.background_tasks.model import (
    BackgroundTaskNotification,
    BgStatus,
    PollFactory,
    TaskMeta,
    TaskType,
)
from mote.orchestration.background_tasks.operation import (
    CoroutineOperation,
    DeferredOperation,
    OperationCancelled,
    OperationFailed,
    OperationPaused,
    OperationSucceeded,
    OperationTimedOut,
    StopDisposition,
    StopReason,
)
from mote.orchestration.background_tasks.status import PAUSE_STATUSES
from mote.runtime.telemetry.logging import log_class, logger

if TYPE_CHECKING:
    from mote.contracts.ports.conversation.message_activity import MessageActivity
    from mote.contracts.ports.conversation.message_sink import MessageSink
    from mote.orchestration.background_tasks.results.store import TaskOutputStore

    class _InboxBuffer(MessageSink, MessageActivity, Protocol):
        """The two buffer slices the pool uses: ``push`` + ``wait_for_message``."""


from mote.orchestration.background_tasks.constants import DEFAULT_MAX_CONCURRENCY as _DEFAULT_MAX_CONCURRENCY
from mote.orchestration.background_tasks.constants import DEFAULT_TASK_TIMEOUT as _DEFAULT_TASK_TIMEOUT
from mote.orchestration.background_tasks.constants import (
    DEFAULT_WAIT_COMPLETION_TIMEOUT as _DEFAULT_WAIT_COMPLETION_TIMEOUT,
)
from mote.orchestration.background_tasks.constants import MAX_TASK_OUTPUT_BYTES_DISPLAY as _OUTPUT_CAP_DISPLAY
from mote.orchestration.background_tasks.delivery import (
    make_progress_writer,
    reset_progress_writer,
    set_progress_writer,
)
from mote.runtime.errors import (
    BackgroundTaskCancelledError,
    BackgroundTaskTimeoutError,
    ErrorReport,
    render_error_block,
)
from mote.runtime.events import bind_telemetry, current_telemetry
from mote.runtime.resources.spill import enforce_tool_result_limit


@log_class(
    level="DEBUG",
    # Hot/trivial state queries polled by the loop — tracing them only adds noise.
    exclude={
        "has_pending",
        "pending_count",
        "pending_ids",
        "get_task_info",
        "list_tasks",
        "list_tasks_for_agent",
    },
)
class BackgroundTaskPool:
    """Pool of background asyncio tasks with event-driven completion notification."""

    def __init__(
        self,
        msg_buffer: "_InboxBuffer",
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        output_store: "Optional[TaskOutputStore]" = None,
        wake: Optional[Callable[[], None]] = None,
        session_id: str = "",
        limit_config: Optional[ToolResultLimitConfig] = None,
        on_terminal_result: Optional[Callable[[TaskMeta], None]] = None,
        retire_result: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._msg_buffer = msg_buffer
        # Result-limit policy — the SAME contract the synchronous ToolExecutor
        # applies to a tool's output. A background result is the deferred tail of
        # a tool result: same policy, different transport (a
        # ``BackgroundTaskNotification`` instead of a ``ToolResult``). Sharing the
        # config type (defaulted identically) means the ``enable``/``persist``
        # toggles + size cap are honored on both transports, so a large whole-task
        # result or a large error block persists+previews here exactly as it would
        # on the sync path — no second policy to keep in sync.
        self._limit_config = limit_config or ToolResultLimitConfig()
        # Owning session id — scopes the on-disk path when a large whole-task
        # *result* is persisted (via ``enforce_tool_result_limit``). Empty falls
        # back to the shared ``default`` bucket, same as the tool-result path.
        self._session_id = session_id
        # Runtime wake callback. ``msg_buffer.push`` wakes mid-turn waiters
        # (its built-in new-message signal), but the scheduler driver parks
        # *between* turns on a separate ``wake_event``; pushing alone won't
        # restart it. So a completion both pushes the notification (delivery)
        # and calls ``wake`` (restart a parked driver). Optional / late-bound
        # via :meth:`set_wake` because the runtime wiring may arrive after the
        # pool is constructed.
        self._wake = wake
        # Push-once result survival + consume-driven GC. A task's whole-task
        # terminal (graph result / agent summary / pause marker) is a push-once
        # signal the model must eventually consume; the live notification can be
        # summarized away by autocompact, so ``on_terminal_result`` registers the
        # result as a re-projectable ResourceUnit and ``retire_result`` unloads it
        # once consumed. Both are late-bound (via :meth:`set_on_terminal_result` /
        # :meth:`set_retire_result`) because the Role wiring — which owns the
        # ResourceRegistry — may be installed after this pool is constructed, and
        # the pool must not import Role (no upward coupling): it only holds the
        # ``Callable`` seams.
        self._on_terminal_result = on_terminal_result
        self._retire_result = retire_result
        # Optional disk-output store. When present, graph tasks submitted with
        # ``progress=True`` get a per-task output sink so node-level
        # ``report_progress`` events land on disk and surface as
        # ``<delta-summary>`` blocks via ``TaskAttachmentGenerator``.
        self._output_store = output_store
        self._tasks: dict[str, asyncio.Task] = {}
        self._operations: dict[str, DeferredOperation] = {}
        self._stop_tasks: set[asyncio.Task] = set()
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
        operation: "DeferredOperation | PollFactory",
        command_name: str,
        timeout: Optional[float] = _DEFAULT_TASK_TIMEOUT,
        task_type: str = TaskType.COROUTINE,
        task_kind: Optional[str] = None,
        agent_id: Optional[str] = None,
        progress: bool = False,
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
        Returns a task_id like ``bg_1``, ``bg_2``, etc.
        """
        deferred = operation if hasattr(operation, "execute") else CoroutineOperation(operation)
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
        self._meta[task_id] = meta
        self._operations[task_id] = deferred

        # Progress sink (innermost wrapper): set the bggraph progress writer in
        # the running task's context before the driver coroutine starts, so node
        # events and the terminal notification land on disk.
        use_progress = progress and self._output_store is not None
        telemetry = current_telemetry() if use_progress else None
        if use_progress:
            self._output_store.init_output(task_id)
            meta.output_path = self._output_store.get_output_path(task_id)
            # Capture the spawner's telemetry runtime now, before the
            # task is created) and hand it to the wrapper explicitly, rather than
            # leaning on the contextvar surviving the create_task boundary.

        async def run_operation():
            coro = deferred.execute()
            if use_progress:
                coro = self._with_progress(coro, task_id, telemetry)
            if timeout is not None and timeout > 0:
                coro = self._execute_with_timeout(deferred, coro, timeout)
            return await coro

        coro = self._run_with_semaphore(run_operation, task_id, deferred)

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

    def set_on_terminal_result(self, cb: Optional[Callable[[TaskMeta], None]]) -> None:
        """Bind (or rebind) the whole-task-terminal result callback.

        Called once per task at its terminal (in ``_on_done``) with the task's
        :class:`TaskMeta`; the Role wires this to register the push-once result
        as a re-projectable ResourceUnit. Late-bound (see ``__init__``) because
        the Role owns the registry and may wire after construction.
        """
        self._on_terminal_result = cb

    def set_retire_result(self, cb: Optional[Callable[[str], None]]) -> None:
        """Bind (or rebind) the consume→retire callback.

        Called with a ``task_id`` when the model consumes a task's result
        (:meth:`mark_retrieved`); the Role wires this to unload the ResourceUnit.
        """
        self._retire_result = cb

    def has_pending(self) -> bool:
        """Return *True* if there are still running tasks."""
        return bool(self._tasks)

    async def wait_for_completion(self, timeout: Optional[float] = _DEFAULT_WAIT_COMPLETION_TIMEOUT) -> bool:
        """Block until the next background task completes, or *timeout* elapses.

        Registers a one-shot completion future and awaits it; the next task to
        finish resolves it. Exposed so collaborators (``Role.wait_interruptible``)
        can await completion without touching pool internals.

        Note: waits for the *next* completion unconditionally — it does NOT
        self-check ``has_pending()``. The ``timeout`` (default 10 min) is a
        safety bound so a bare call on an idle/empty pool returns instead of
        blocking forever; pass ``None`` to wait without a bound. Callers that
        already bound the wait themselves (e.g. ``wait_interruptible`` races it
        against a new-message signal) can ignore it.

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

    def get_outcome(self, task_id: str):
        """Return the background-owned terminal outcome, if available."""
        meta = self._meta.get(task_id)
        return meta.outcome if meta is not None else None

    def list_tasks(self) -> list[TaskMeta]:
        """Return metadata for all tracked tasks (running + recently completed)."""
        return list(self._meta.values())

    def mark_retrieved(self, task_id: str) -> None:
        """Record that the model has consumed a task's push-once result.

        The consume tools (GetNodeState / resume / cancel) call this on a
        *successful* consume. It flips ``meta.retrieved``, retires the
        re-projected ResourceUnit (so it stops re-surfacing after compaction),
        and reaps the now-fully-consumed meta from the tracking dict — the "real
        consume" half of the double-safety recycle. Unknown ids are ignored.
        """
        meta = self._meta.get(task_id)
        if meta is None:
            return
        # A resumable pause is NOT consumed by mere inspection — its resume
        # marker must keep re-surfacing until the task is actually resumed
        # (``resubmit`` flips it to RUNNING first, so the resume path reaches
        # here with a non-pause status and does retire the old marker).
        if meta.status in PAUSE_STATUSES:
            return
        meta.retrieved = True
        if self._retire_result is not None:
            try:
                self._retire_result(task_id)
            except Exception as exc:  # noqa: BLE001 — retire is best-effort
                logger.debug(f"BackgroundTaskPool: retire_result failed for {task_id}: {exc}")
        self._maybe_reap_meta(task_id, meta)

    def _maybe_reap_meta(self, task_id: str, meta: TaskMeta) -> None:
        """Drop a fully-consumed terminal task's meta so ``_meta`` stays bounded.

        Reaped only when the task is genuinely done with: consumed
        (``retrieved``), no longer running (absent from ``_tasks``), and not a
        resumable pause (a pause keeps its snapshot for ``resume_tasks``, so it
        survives even if ``retrieved`` — resume clears the pause first). This is
        the shared reap point for both the consume path and the round-based
        recycle.
        """
        if not meta.retrieved:
            return
        if task_id in self._tasks:
            return
        if meta.status in PAUSE_STATUSES:
            return
        self._meta.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        """Cancel a running background task.

        Returns ``True`` if the task was found and cancel was requested,
        ``False`` if the task_id is unknown or already finished.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        operation = self._operations.get(task_id)
        meta = self._meta.get(task_id)
        if meta is not None:
            meta.status = BgStatus.CANCELLED
        if operation is not None:
            stopper = asyncio.create_task(self._request_operation_stop(task_id, operation, StopReason.USER_CANCEL))
            self._stop_tasks.add(stopper)
            stopper.add_done_callback(self._stop_tasks.discard)
        else:
            task.cancel()
        return True

    def cancel_for_cap(self, task_id: str) -> bool:
        """Cancel a task because its output exceeded the disk size cap.

        Sets a flag so ``_on_done`` includes the cap reason in ``result``,
        then delegates to :meth:`cancel`.
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
        operation: "DeferredOperation | PollFactory",
        *,
        timeout: Optional[float] = None,
        progress: bool = True,
    ) -> str:
        """Re-submit a poll factory under an existing task_id (for resume/retry).

        Resets the task's status to RUNNING and attaches a fresh asyncio.Task.
        The existing generic ``TaskMeta`` is preserved.

        Args:
            task_id: An existing task_id previously returned by :meth:`submit`.
            poll_factory: A :data:`PollFactory` callable that returns a coroutine.
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
        # A resumed run produces a fresh terminal, so reset the push-once
        # bookkeeping: retire the now-stale pointer (a pause marker or a prior
        # result) so it stops re-surfacing while the task runs again, and clear
        # the flags so the next terminal re-registers a fresh result pointer.
        if meta.registered_resource and self._retire_result is not None:
            try:
                self._retire_result(task_id)
            except Exception as exc:  # noqa: BLE001 — retire is best-effort
                logger.debug(f"BackgroundTaskPool: retire_result failed on resubmit for {task_id}: {exc}")
        meta.registered_resource = False
        meta.retrieved = False

        deferred = operation if hasattr(operation, "execute") else CoroutineOperation(operation)
        use_progress = progress and self._output_store is not None
        telemetry = current_telemetry() if use_progress else None

        async def run_operation():
            coro = deferred.execute()
            if use_progress:
                coro = self._with_progress(coro, task_id, telemetry)
            if timeout is not None and timeout > 0:
                coro = self._execute_with_timeout(deferred, coro, timeout)
            return await coro

        coro = self._run_with_semaphore(run_operation, task_id, deferred)

        task = asyncio.create_task(coro)
        self._tasks[task_id] = task
        self._operations[task_id] = deferred
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

    async def aclose(self) -> None:
        """Cancel and join every owned task, waiter, and output drain."""

        stop_calls = [
            self._request_operation_stop(task_id, operation, StopReason.SHUTDOWN)
            for task_id, operation in tuple(self._operations.items())
        ]
        if stop_calls:
            await asyncio.gather(*stop_calls, return_exceptions=True)
        tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._stop_tasks:
            await asyncio.gather(*tuple(self._stop_tasks), return_exceptions=True)
            self._stop_tasks.clear()
        self._tasks.clear()
        waiters, self._completion_waiters = self._completion_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        self._wake = None
        if self._output_store is not None:
            await self._output_store.aclose()

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

    async def _run_with_semaphore(
        self,
        runner: Callable[[], Coroutine],
        task_id: str,
        operation: DeferredOperation | None = None,
    ):
        """Acquire concurrency capacity before creating the operation coroutine."""
        try:
            async with self._semaphore:
                meta = self._meta.get(task_id)
                if meta is not None and meta.status == BgStatus.PENDING:
                    meta.status = BgStatus.RUNNING
                    meta.start_time = time.time()
                return await runner()
        finally:
            if operation is not None:
                await self._close_operation(operation)

    @staticmethod
    async def _close_operation(operation: DeferredOperation) -> None:
        close_task = asyncio.create_task(operation.aclose())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await close_task
            raise

    async def _request_operation_stop(
        self,
        task_id: str,
        operation: DeferredOperation,
        reason: StopReason,
    ) -> None:
        task = self._tasks.get(task_id)
        try:
            await asyncio.wait_for(
                operation.request_stop(reason, StopDisposition.CHECKPOINT),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, Exception):
            if task is not None and not task.done():
                task.cancel()
            return
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()

    @staticmethod
    async def _with_timeout(coro: Coroutine, timeout: float):
        """Wrap *coro* so it raises ``asyncio.TimeoutError`` after *timeout* seconds."""
        return await asyncio.wait_for(coro, timeout=timeout)

    @staticmethod
    async def _execute_with_timeout(
        operation: DeferredOperation,
        coro: Coroutine,
        timeout: float,
    ):
        execution = asyncio.create_task(coro)
        try:
            return await asyncio.wait_for(asyncio.shield(execution), timeout=timeout)
        except asyncio.TimeoutError:
            outcome = await operation.request_stop(
                StopReason.TIMEOUT,
                StopDisposition.CHECKPOINT,
            )
            if not execution.done():
                execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            return outcome

    async def _with_progress(self, coro: Coroutine, task_id: str, telemetry=None):
        """Run *coro* with the bggraph progress writer bound to this task.

        The writer renders each ``report_progress`` event and appends it to the
        task's disk output. The contextvar is set inside the running task so it
        propagates to the driver coroutine and the node tasks it spawns.

        ``telemetry`` is captured synchronously at spawn time and explicitly
        re-bound inside the spawned task with :func:`bind_telemetry`, so progress
        observations reach the correct runtime by
        an explicit hand-off, not by relying on ``create_task`` snapshotting the
        contextvar across the spawn boundary. Pure observation, so losing it
        could only ever drop a progress mirror, never a control veto.
        """

        # Both call sites gate on ``self._output_store is not None`` before
        # wrapping progress, so it is always present here; narrow it.
        store = self._output_store
        assert store is not None, "_with_progress requires an output store"
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
            with bind_telemetry(telemetry):
                return await coro
        finally:
            reset_progress_writer(token)

    @staticmethod
    def _build_xml(
        task_id: str,
        command_name: str,
        status: str,
        summary: str,
        result: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        """Build a ``<task-notification>`` XML envelope."""
        lines = [
            "<task-notification>",
            f"<task-id>{task_id}</task-id>",
            f"<command>{_escape_xml(command_name)}</command>",
            f"<status>{status}</status>",
            f"<summary>{_escape_xml(summary)}</summary>",
        ]
        if result is not None:
            lines.append(f"<result>{_escape_xml(result)}</result>")
        # Surface the streaming stdout log path so the model can Read the full
        # process log on demand. This is the task's *process* output, distinct
        # from the ``<result>`` produced value (which persists to its own
        # ``.tool_results`` file when large) — both pointers can coexist.
        if output_path is not None:
            lines.append(f"<output-path>{_escape_xml(output_path)}</output-path>")
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
        except Exception as exc:  # noqa: BLE001 — best-effort wake
            logger.debug(f"BackgroundTaskPool: runtime wake failed (delivery already queued): {exc}")

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
        except Exception as exc:  # noqa: BLE001 — delivery must never break the pipeline
            logger.debug(f"BackgroundTaskPool: terminal notification delivery failed: {exc}")
            return
        self._wake_runtime()

    def _limit_block(self, output: str, command_name: str, task_id: str) -> str:
        """Apply the shared result-limit policy to a terminal text block.

        The single scoping point for a background task's outgoing text — the
        SUCCESS result *and* every error/timeout/cancel block route through here,
        so both transports (sync tool + bg task) obey one policy:

        - ``enable_tool_result_limit`` off → the block is passed through whole
          (no cap, no persistence), same as the ToolExecutor's early-out.
        - otherwise the block is capped at ``default_max_result_size_chars``;
          over the threshold it is persisted to a session-scoped
          ``.tool_results`` file (when ``persist_large_tool_results``) and
          replaced by a ``<persisted-output>`` preview, else head-truncated with
          a dropped-size notice.

        Result-scoping only — compression + media handling stay in
        ``ToolSettlement`` (genuinely sync-tool-transport concerns), so
        there is no upward coupling of the pool onto the executor.
        """
        cfg = self._limit_config
        if not cfg.enable_tool_result_limit or not output:
            return output
        return enforce_tool_result_limit(
            output,
            command_name,
            result_id=f"task-{task_id}",
            session_id=self._session_id,
            max_result_size_chars=cfg.default_max_result_size_chars,
            persist=cfg.persist_large_tool_results,
            store=self._output_store.store if self._output_store is not None else None,
        )

    def _on_done(self, task_id: str, command_name: str, task: asyncio.Task) -> None:
        """Synchronous callback invoked by the event loop when a task finishes.

        Pushes the structured completion notification directly into the
        agent's msg_buffer (NEXT priority) and wakes the runtime so a parked
        scheduler driver starts a new react turn. No Telemetry round-trip:
        completion is a pure observation with a single consumer, so it goes
        straight to the queue the react loop observes.
        """

        status: str
        operation_outcome = None
        result: Optional[str] = None
        summary: str
        error_dict: Optional[dict] = None

        if task.cancelled():
            operation_outcome = OperationCancelled()
            status = BgStatus.CANCELLED
            meta = self._meta.get(task_id)
            if meta is not None and meta._output_capped:
                summary = f"{command_name} was killed because its output exceeded the disk size limit."
                cancel_msg = f"Background command killed: output exceeded {_OUTPUT_CAP_DISPLAY} disk cap."
            else:
                summary = f"{command_name} was cancelled."
                cancel_msg = summary
            # Synthesize a typed error so a cancellation surfaces the same
            # structured <error> block as every other terminal outcome.
            report = ErrorReport.from_exception(BackgroundTaskCancelledError(cancel_msg))
            error_dict = report.as_dict()
            result = self._limit_block(render_error_block(report), command_name, task_id)
        else:
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, asyncio.TimeoutError):
                    operation_outcome = OperationTimedOut()
                    status = BgStatus.TIMEOUT
                    summary = f"{command_name} timed out after exceeding the time limit."
                    # Route timeout through the shared contract too (it was a
                    # bypass before — error_dict stayed None), so the model gets
                    # the uniform block + machine-readable report.
                    report = ErrorReport.from_exception(BackgroundTaskTimeoutError(summary))
                    error_dict = report.as_dict()
                    result = self._limit_block(render_error_block(report), command_name, task_id)
                else:
                    operation_outcome = OperationFailed(exc)
                    status = BgStatus.FAILED
                    # Normalize through the shared error contract instead of
                    # dumping a raw traceback: the model gets a uniform <error>
                    # block (code/recovery/structured detail), and the machine-
                    # readable report rides along on the notification's `error`.
                    report = ErrorReport.from_exception(exc)
                    error_dict = report.as_dict()
                    result = self._limit_block(render_error_block(report), command_name, task_id)
                    summary = f"{command_name} failed."
            else:
                raw = task.result()
                meta = self._meta.get(task_id)
                if meta is not None and meta.status == BgStatus.CANCELLED:
                    raw = OperationCancelled()
                # Compatibility factories are normalized by CoroutineOperation;
                # unwrap their successful value before applying the pool's
                # existing result formatting. Native DeferredOperation
                # implementations still return the structured variants below.
                if isinstance(raw, OperationSucceeded):
                    raw = raw.output
                if isinstance(raw, OperationFailed):
                    operation_outcome = raw
                    status = BgStatus.FAILED
                    report = ErrorReport.from_exception(raw.error)
                    error_dict = report.as_dict()
                    result = self._limit_block(render_error_block(report), command_name, task_id)
                    summary = f"{command_name} failed."
                elif isinstance(raw, OperationPaused):
                    operation_outcome = raw
                    status = BgStatus.STALLED if raw.reason == "stall" else BgStatus.WAITING_FOR_ROUTE
                    result = raw.reason
                    summary = f"{command_name} paused, awaiting a decision."
                elif isinstance(raw, OperationTimedOut):
                    operation_outcome = raw
                    status = BgStatus.TIMEOUT
                    summary = f"{command_name} timed out."
                    report = ErrorReport.from_exception(BackgroundTaskTimeoutError(summary))
                    error_dict = report.as_dict()
                    result = self._limit_block(render_error_block(report), command_name, task_id)
                elif isinstance(raw, OperationCancelled):
                    operation_outcome = raw
                    status = BgStatus.CANCELLED
                    meta = self._meta.get(task_id)
                    if meta is not None and meta._output_capped:
                        summary = f"{command_name} was killed because its output exceeded the disk size limit."
                        reason = f"Background command killed: output exceeded {_OUTPUT_CAP_DISPLAY} disk cap."
                    else:
                        summary = f"{command_name} was cancelled."
                        reason = raw.reason
                    report = ErrorReport.from_exception(BackgroundTaskCancelledError(reason))
                    error_dict = report.as_dict()
                    result = self._limit_block(render_error_block(report), command_name, task_id)
                else:
                    operation_outcome = OperationSucceeded(raw)
                    status = BgStatus.SUCCESS
                    # A whole-task result is a tool output like any other, so it
                    # rides the SAME size-limit primitive the ToolExecutor uses
                    # on the synchronous path: under the threshold it is inlined
                    # whole (lets the model act in one shot); over it, the full
                    # result is persisted to a session-scoped ``.tool_results``
                    # file and the inline ``<result>`` becomes a
                    # ``<persisted-output>`` preview + path. That result file is
                    # distinct from the streaming stdout log at ``output_path``
                    # (process log vs. produced value) — both pointers coexist.
                    raw_result = str(raw) if raw is not None else "(no output)"
                    result = self._limit_block(raw_result, command_name, task_id)
                    summary = f"{command_name} completed successfully."

        # Update task metadata.
        meta = self._meta.get(task_id)
        if meta is not None:
            meta.outcome = operation_outcome
            meta.status = status
            meta.end_time = time.time()
            meta.result = result
            meta.error = error_dict
            meta.notified = True

        output_path = meta.output_path if meta is not None else None
        body = self._build_xml(
            task_id,
            command_name,
            status,
            summary,
            result=result,
            output_path=output_path,
        )

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

        # Register the push-once result for post-compaction re-projection. This
        # runs AFTER deliver (the live notification is the model's first sight of
        # the result); the registered pointer only re-surfaces once compaction
        # discards that notification. Best-effort — a registration failure must
        # never break the completion path. Idempotent via ``registered_resource``
        # so a resubmit→re-terminal does not double-load; skipped when the model
        # already consumed the task (e.g. cancelled a running one) — no point
        # re-projecting a result that was already acted on.
        if (
            self._on_terminal_result is not None
            and meta is not None
            and not meta.registered_resource
            and not meta.retrieved
        ):
            try:
                self._on_terminal_result(meta)
            except Exception as exc:  # noqa: BLE001 — registration is best-effort
                logger.debug(f"BackgroundTaskPool: on_terminal_result failed for {task_id}: {exc}")

        # Resolve every registered one-shot completion future (fan-out
        # broadcast). Iterate a snapshot so a re-entrant completion is safe;
        # each resolved/cancelled future removes itself via _discard_waiter.
        for fut in list(self._completion_waiters):
            if not fut.done():
                fut.set_result(None)

        # Remove from tracking dict, then reap the meta if the task was already
        # consumed before its terminal (a cancel of a running task marks
        # ``retrieved`` before ``_on_done`` fires) — keeps ``_meta`` bounded.
        self._tasks.pop(task_id, None)
        self._operations.pop(task_id, None)
        if meta is not None:
            self._maybe_reap_meta(task_id, meta)
