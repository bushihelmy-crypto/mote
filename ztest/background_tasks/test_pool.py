#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.tasks.pool.BackgroundTaskPool`.

Covers task submission and id allocation, the success / failure / timeout /
cancellation completion paths, metadata bookkeeping, the per-agent helpers, the
concurrency semaphore, the event-driven waiters (``wait_all`` /
``wait_for_completion`` / ``wait_any``), task adoption, the cap-cancel path, the
``<task-notification>`` XML envelope, and the structured notification pushed
into the message buffer.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import MessagePriority
from mote.contracts.session.identity import SessionId
from mote.contracts.task.models import AttemptId
from mote.orchestration.background_tasks import (
    BackgroundTaskNotification,
    BackgroundTaskPool,
    BackgroundTaskStatus,
    TaskType,
)
from mote.orchestration.background_tasks.constants import MAX_RESULT_LEN
from mote.orchestration.background_tasks.operation import (
    OperationCancelled,
    OperationSucceeded,
    StopDisposition,
    StopReason,
)

from .conftest import boom, echo, forever, gated, started_gated, wait_started


async def _drain(pool):
    """Cancel every pending task and wait for the pool to empty."""
    for tid in pool.pending_ids:
        pool.cancel(tid)
    await pool.wait_all()


@pytest.mark.asyncio
async def test_aclose_cancels_and_joins_all_owned_tasks(pool) -> None:
    pool.submit(lambda: forever(), "one", timeout=None)
    pool.submit(lambda: forever(), "two", timeout=None)
    await asyncio.sleep(0)

    await pool.aclose()
    await pool.aclose()

    assert pool.has_pending() is False


class RecordingOperation:
    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.release = release
        self.stop_calls: list[tuple[StopReason, StopDisposition]] = []
        self.close_calls = 0

    async def execute(self):
        if self.release is not None:
            await self.release.wait()
        return OperationSucceeded("done")

    async def request_stop(self, reason, disposition):
        self.stop_calls.append((reason, disposition))
        if self.release is not None:
            self.release.set()
        return OperationCancelled(reason.value)

    async def aclose(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_operation_closes_exactly_once_after_success(pool) -> None:
    operation = RecordingOperation()
    pool.submit(operation, "operation", timeout=None)
    await pool.wait_all()
    assert operation.close_calls == 1


@pytest.mark.asyncio
async def test_operation_cancel_requests_cooperative_stop_before_close(pool) -> None:
    release = asyncio.Event()
    operation = RecordingOperation(release)
    task_id = pool.submit(operation, "operation", timeout=None)
    await asyncio.sleep(0)
    assert pool.cancel(task_id)
    await pool.wait_all()
    assert operation.stop_calls == [(StopReason.USER_CANCEL, StopDisposition.CHECKPOINT)]
    assert operation.close_calls == 1


class TestSubmitAndIds:
    @pytest.mark.asyncio
    async def test_submit_returns_incrementing_ids(self, pool):
        a = pool.submit(lambda: forever(), "a", timeout=None)
        b = pool.submit(lambda: forever(), "b", timeout=None)
        assert a == "bg_1"
        assert b == "bg_2"
        assert pool.has_pending() is True
        assert pool.pending_count == 2
        assert set(pool.pending_ids) == {"bg_1", "bg_2"}
        await _drain(pool)

    @pytest.mark.asyncio
    async def test_meta_recorded_on_submit(self, pool):
        tid = pool.submit(lambda: forever(), "do-thing", timeout=None, task_kind="bash", agent_id="A1")
        meta = pool.get_task_info(tid)
        assert meta is not None
        assert meta.command_name == "do-thing"
        assert meta.task_kind == "bash"
        assert meta.agent_id == "A1"
        # status is PENDING until the coroutine actually starts running.
        assert meta.status == BackgroundTaskStatus.PENDING
        await _drain(pool)

    def test_get_task_info_unknown(self, pool):
        assert pool.get_task_info("nope") is None


class TestCompletionPaths:
    @pytest.mark.asyncio
    async def test_success(self, pool, msg_buffer):
        tid = pool.submit(lambda: echo("hello"), "echo")
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.SUCCESS
        assert meta.result == "hello"
        assert meta.notified is True
        assert meta.end_time is not None
        assert pool.has_pending() is False
        assert pool.get_task_info(tid) is not None  # retained after completion

    @pytest.mark.asyncio
    async def test_none_result_becomes_placeholder(self, pool):
        tid = pool.submit(lambda: echo(None), "noop")
        await pool.wait_all()
        assert pool.get_task_info(tid).result == "(no output)"

    @pytest.mark.asyncio
    async def test_result_delivered_in_full(self, pool):
        # A result under the size-limit threshold is delivered inline whole, so
        # the model gets the output up front (needn't poll GetNodeState). This
        # size (a few KB) stays well under DEFAULT_MAX_RESULT_SIZE_CHARS.
        big = "x" * (MAX_RESULT_LEN + 500)
        tid = pool.submit(lambda: echo(big), "big")
        await pool.wait_all()
        result = pool.get_task_info(tid).result
        assert result == big
        assert "truncated" not in result

    @pytest.mark.asyncio
    async def test_large_result_persists_and_previews(self, tmp_path):
        # A whole-task result over DEFAULT_MAX_RESULT_SIZE_CHARS rides the same
        # size-limit primitive as the synchronous tool path: the full value is
        # written to a session-scoped ``tool_results/`` file and the inline
        # result becomes a ``<persisted-output>`` preview naming that path —
        # distinct from the streaming stdout log at ``task_outputs/`` (both
        # co-located under the session directory).
        from mote.contracts.config.tool import DEFAULT_MAX_RESULT_SIZE_CHARS, PERSISTED_OUTPUT_OPEN_TAG
        from mote.contracts.conversation import MessageQueue
        from mote.orchestration.background_tasks import TaskOutputStore

        store = TaskOutputStore(
            base_dir=tmp_path,
            session_id=SessionId("background-task-test"),
        )
        pool = BackgroundTaskPool(MessageQueue(), output_store=store, session_id="s1")
        huge = "y" * (DEFAULT_MAX_RESULT_SIZE_CHARS + 1000)
        tid = pool.submit(lambda: echo(huge), "huge")
        await pool.wait_all()
        result = pool.get_task_info(tid).result
        assert result.startswith(PERSISTED_OUTPUT_OPEN_TAG)
        assert "Output too large" in result
        # Full value landed in the session-scoped tool-results file.
        result_file = tmp_path / ".agent_sessions" / "s1" / "tool_results" / f"task-{tid}.txt"
        assert result_file.exists()
        assert result_file.read_text() == huge

    @pytest.mark.asyncio
    async def test_failure_records_error_report(self, pool):
        tid = pool.submit(lambda: boom(ValueError("kaboom")), "fail")
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.FAILED
        # Result is the uniform <error> block; the typed report is on meta.error.
        assert meta.result.startswith("<error ")
        assert "kaboom" in meta.result
        assert meta.error is not None
        assert meta.error.error == "ValueError"
        assert meta.error.code == "UNKNOWN"
        assert meta.error.message == "kaboom"

    @pytest.mark.asyncio
    async def test_timeout(self, pool):
        tid = pool.submit(lambda: forever(), "hang", timeout=0.02)
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BackgroundTaskStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_carries_structured_error(self, pool):
        # Timeout now routes through the shared ErrorReport contract: the
        # uniform <error> block lands in the result and the machine-readable
        # report rides on meta.error (it was a bypass before — error was None).
        tid = pool.submit(lambda: forever(), "hang", timeout=0.02)
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.TIMEOUT
        assert meta.result.startswith("<error ")
        assert 'code="BG_TASK_TIMEOUT"' in meta.result
        assert meta.error is not None
        assert meta.error.code == "BG_TASK_TIMEOUT"
        # Timeout is retryable.
        assert 'retryable="true"' in meta.result

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, pool):
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "g", timeout=None)
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BackgroundTaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_carries_structured_error(self, pool):
        # Cancellation also routes through the shared contract: a uniform
        # <error> block + machine-readable report on meta.error (abort/non-
        # retryable), no longer a bare summary string.
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "g", timeout=None)
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.CANCELLED
        assert meta.result.startswith("<error ")
        assert 'code="BG_TASK_CANCELLED"' in meta.result
        assert meta.error is not None
        assert meta.error.code == "BG_TASK_CANCELLED"
        assert 'retryable="false"' in meta.result

    def test_cancel_unknown_returns_false(self, pool):
        assert pool.cancel("nope") is False


class TestNotification:
    @pytest.mark.asyncio
    async def test_success_notification_pushed(self, pool, msg_buffer):
        tid = pool.submit(lambda: echo("R"), "cmd")
        await pool.wait_all()
        msgs = msg_buffer.pop_all()
        assert len(msgs) == 1
        notif = msgs[0]
        assert isinstance(notif, BackgroundTaskNotification)
        assert notif.task_id == tid
        assert notif.command_name == "cmd"
        assert notif.status == BackgroundTaskStatus.SUCCESS
        assert notif.result == "R"
        assert "<task-notification>" in notif.content

    @pytest.mark.asyncio
    async def test_notification_priority_is_next(self, pool, msg_buffer):
        pool.submit(lambda: echo("R"), "cmd")
        await pool.wait_all()
        # NOW-priority pop should not surface a NEXT notification.
        assert msg_buffer.pop(max_priority=MessagePriority.NOW) is None
        assert msg_buffer.pop(max_priority=MessagePriority.NEXT) is not None


class TestProgressTaskTermination:
    """A progress task whose driver is interrupted (timeout / external cancel)
    never reports a terminal from inside the graph, so ``_on_done`` must still
    deliver one — the agent must always learn the task ended.

    Regression: the old ``_progress_active`` flag suppressed ``_on_done`` for
    *all* progress tasks, silently dropping the terminal on these paths.
    """

    def _progress_pool(self, msg_buffer, tmp_path):
        from mote.orchestration.background_tasks.results.store import TaskOutputStore

        return BackgroundTaskPool(
            msg_buffer,
            output_store=TaskOutputStore(
                tmp_path,
                session_id=SessionId("background-task-test"),
            ),
            session_id="background-task-test",
        )

    @pytest.mark.asyncio
    async def test_progress_task_timeout_still_delivers_terminal(self, msg_buffer, tmp_path):
        pool = self._progress_pool(msg_buffer, tmp_path)
        # forever() never calls report_progress, so the in-graph writer never
        # delivers a terminal; the timeout must produce one via _on_done.
        tid = pool.submit(lambda: forever(), "hang", timeout=0.02, progress=True)
        await pool.wait_all()
        msgs = [m for m in msg_buffer.pop_all() if isinstance(m, BackgroundTaskNotification)]
        terminals = [m for m in msgs if m.task_terminal]
        assert len(terminals) == 1
        assert terminals[0].task_id == tid
        assert terminals[0].status == BackgroundTaskStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_progress_task_cancel_still_delivers_terminal(self, msg_buffer, tmp_path):
        pool = self._progress_pool(msg_buffer, tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "g", timeout=None, progress=True)
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        terminals = [m for m in msg_buffer.pop_all() if isinstance(m, BackgroundTaskNotification) and m.task_terminal]
        assert len(terminals) == 1
        assert terminals[0].status == BackgroundTaskStatus.CANCELLED


class TestDeliver:
    """The single push+wake choke point all notification producers route through."""

    def test_deliver_rejects_unknown_task_attempt_without_wake(self, msg_buffer):
        wakes = []
        pool = BackgroundTaskPool(msg_buffer, wake=lambda: wakes.append(1))
        notification = BackgroundTaskNotification(
            content="stale",
            task_id="bg_1",
            attempt_id=AttemptId(1),
            status=BackgroundTaskStatus.SUCCESS,
        )
        assert pool.deliver(notification) is False
        assert msg_buffer.pop_all() == []
        assert wakes == []

    def test_deliver_swallows_push_failure(self):
        class _BadSink:
            def push(self, *a, **k):
                raise RuntimeError("queue is gone")

        wakes = []
        pool = BackgroundTaskPool(_BadSink(), wake=lambda: wakes.append(1))
        # A delivery failure must never break the pipeline — and must not wake.
        pool.deliver(BackgroundTaskNotification(content="x"))
        assert wakes == []


class TestBuildXml:
    def test_with_result_and_escaping(self):
        xml = BackgroundTaskPool._build_xml("bg_1", "echo & ls", "success", "done", result="a<b>")
        assert "<task-id>bg_1</task-id>" in xml
        assert "<command>echo &amp; ls</command>" in xml
        assert "<status>success</status>" in xml
        assert "<summary>done</summary>" in xml
        assert "<result>a&lt;b&gt;</result>" in xml
        assert xml.startswith("<task-notification>")
        assert xml.endswith("</task-notification>")

    def test_without_result_omits_tag(self):
        xml = BackgroundTaskPool._build_xml("bg_1", "cmd", "cancelled", "gone")
        assert "<result>" not in xml

    def test_with_output_path(self):
        xml = BackgroundTaskPool._build_xml(
            "bg_1", "cmd", "success", "done", result="r", output_path="/w/.task_outputs/bg_1.output"
        )
        assert "<output-path>/w/.task_outputs/bg_1.output</output-path>" in xml

    def test_without_output_path_omits_tag(self):
        xml = BackgroundTaskPool._build_xml("bg_1", "cmd", "success", "done", result="r")
        assert "<output-path>" not in xml


class TestWaiters:
    @pytest.mark.asyncio
    async def test_wait_all_blocks_until_done(self, pool):
        ev = asyncio.Event()
        pool.submit(lambda: gated(ev, "v"), "g", timeout=None)
        waiter = asyncio.create_task(pool.wait_all())
        await asyncio.sleep(0)
        assert not waiter.done()
        ev.set()
        await asyncio.wait_for(waiter, timeout=1)
        assert pool.has_pending() is False

    @pytest.mark.asyncio
    async def test_wait_for_completion_true(self, pool):
        ev = asyncio.Event()
        pool.submit(lambda: gated(ev, "v"), "g", timeout=None)
        waiter = asyncio.create_task(pool.wait_for_completion(timeout=2))
        await asyncio.sleep(0)  # let the waiter register before completion
        ev.set()
        assert await asyncio.wait_for(waiter, timeout=1) is True

    @pytest.mark.asyncio
    async def test_wait_for_completion_timeout_false(self, pool):
        # Idle pool, short bound -> returns False without blocking forever.
        assert await pool.wait_for_completion(timeout=0.02) is False

    @pytest.mark.asyncio
    async def test_wait_any_task_done(self, pool):
        ev = asyncio.Event()
        pool.submit(lambda: gated(ev, "v"), "g", timeout=None)
        waiter = asyncio.create_task(pool.wait_any(timeout=1))
        await asyncio.sleep(0)
        ev.set()
        assert await asyncio.wait_for(waiter, timeout=1) == "task_done"

    @pytest.mark.asyncio
    async def test_wait_any_new_message(self, pool, msg_buffer):
        waiter = asyncio.create_task(pool.wait_any(timeout=1))
        await asyncio.sleep(0)
        from mote.contracts.conversation import UserMessage

        msg_buffer.push(UserMessage(content="ping"))
        assert await asyncio.wait_for(waiter, timeout=1) == "new_message"

    @pytest.mark.asyncio
    async def test_wait_any_timeout(self, pool):
        assert await pool.wait_any(timeout=0.02) == "timeout"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_running(self, msg_buffer):
        pool = BackgroundTaskPool(msg_buffer, max_concurrency=1)
        started_a, release_a = asyncio.Event(), asyncio.Event()
        started_b, release_b = asyncio.Event(), asyncio.Event()
        a = pool.submit(lambda: started_gated(started_a, release_a), "a", timeout=None)
        b = pool.submit(lambda: started_gated(started_b, release_b), "b", timeout=None)

        await wait_started(started_a)
        # A holds the only slot; B is still queued (PENDING).
        assert pool.get_task_info(a).status == BackgroundTaskStatus.RUNNING
        assert pool.get_task_info(b).status == BackgroundTaskStatus.PENDING
        assert started_b.is_set() is False

        release_a.set()
        await wait_started(started_b)
        assert pool.get_task_info(b).status == BackgroundTaskStatus.RUNNING
        release_b.set()
        await pool.wait_all()


class TestAdopt:
    @pytest.mark.asyncio
    async def test_adopt_tracks_running_task(self, pool):
        ev = asyncio.Event()
        task = asyncio.create_task(gated(ev, "adopted-out"))
        tid = pool.adopt(task, command_name="adopted")
        assert tid == "bg_1"
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.RUNNING  # already executing, no PENDING phase
        assert meta.command_name == "adopted"
        ev.set()
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.SUCCESS
        assert meta.result == "adopted-out"


class TestCapCancel:
    @pytest.mark.asyncio
    async def test_cancel_for_cap_marks_and_reports(self, pool):
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "huge", timeout=None)
        await wait_started(started)
        assert pool.cancel_for_cap(tid) is True
        assert pool.get_task_info(tid).output_capped is True
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BackgroundTaskStatus.CANCELLED
        assert "disk cap" in meta.result

    def test_cancel_for_cap_unknown(self, pool):
        assert pool.cancel_for_cap("nope") is False


class TestAgentScoping:
    @pytest.mark.asyncio
    async def test_list_and_cancel_for_agent(self, pool):
        ev = asyncio.Event()
        pool.submit(lambda: gated(ev), "a1", timeout=None, agent_id="A")
        pool.submit(lambda: gated(ev), "a2", timeout=None, agent_id="A")
        pool.submit(lambda: gated(ev), "b1", timeout=None, agent_id="B")
        # Let all three acquire the semaphore and start awaiting the gate.
        for _ in range(50):
            if all(pool.get_task_info(t).status == BackgroundTaskStatus.RUNNING for t in ("bg_1", "bg_2", "bg_3")):
                break
            await asyncio.sleep(0)

        assert {m.task_id for m in pool.list_tasks_for_agent("A")} == {"bg_1", "bg_2"}
        assert {m.task_id for m in pool.list_tasks_for_agent("B")} == {"bg_3"}

        cancelled = pool.cancel_tasks_for_agent("A")
        assert set(cancelled) == {"bg_1", "bg_2"}
        ev.set()
        await pool.wait_all()
        assert pool.get_task_info("bg_1").status == BackgroundTaskStatus.CANCELLED
        assert pool.get_task_info("bg_3").status == BackgroundTaskStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_list_tasks_returns_all(self, pool):
        pool.submit(lambda: echo(), "a")
        pool.submit(lambda: echo(), "b")
        await pool.wait_all()
        assert {m.task_id for m in pool.list_tasks()} == {"bg_1", "bg_2"}


class TestTaskTypeDefault:
    @pytest.mark.asyncio
    async def test_default_task_type_is_coroutine(self, pool):
        tid = pool.submit(lambda: forever(), "c", timeout=None)
        assert pool.get_task_info(tid).task_type == TaskType.COROUTINE
        await _drain(pool)


class TestProgressTelemetryVisibility:
    """Pin down the contextvar visibility: a progress event reported inside a
    submitted task is mirrored onto telemetry bound when the task was created."""

    @pytest.mark.asyncio
    async def test_report_progress_reaches_telemetry_handler(self, msg_buffer, tmp_path):
        from mote.contracts.events.task import TaskProgressEvent
        from mote.contracts.ports.events.telemetry import (
            TelemetryIdentity,
            TelemetryOverflow,
            TelemetrySubscriptionSpec,
        )
        from mote.contracts.task.progress import ActivityProgressEvent, ActivityProgressIdentity, ProgressPhase
        from mote.orchestration.background_tasks import TaskOutputStore
        from mote.orchestration.workflows.events import report_progress
        from mote.runtime.events import AllTelemetryBinding, TelemetryManifest, TelemetryRuntime, bind_telemetry

        class _Recorder:
            def __init__(self):
                self.events = []

            def handle_sync(self, event):
                if isinstance(event, TaskProgressEvent):
                    self.events.append(event)

            async def handle(self, event):
                return None

        rec = _Recorder()
        telemetry = TelemetryRuntime(
            TelemetryManifest(
                (
                    AllTelemetryBinding(
                        TelemetrySubscriptionSpec(
                            identity=TelemetryIdentity("mote.test.task_progress"),
                            capacity=16,
                            overflow=TelemetryOverflow.DROP_NEWEST,
                        ),
                        rec,
                        sync_handler=rec,
                    ),
                )
            )
        )
        telemetry.start()

        pool = BackgroundTaskPool(
            msg_buffer,
            output_store=TaskOutputStore(
                base_dir=tmp_path,
                session_id=SessionId("session"),
            ),
        )

        async def reporter():
            report_progress(
                ActivityProgressEvent(
                    ActivityProgressIdentity("activity", "definition"),
                    "split",
                    ProgressPhase.RUNNING,
                    "hello",
                )
            )
            return "ok"

        with bind_telemetry(telemetry):
            tid = pool.submit(lambda: reporter(), "rep", progress=True)
            await pool.wait_all()
        await telemetry.drain()

        assert tid == "bg_1"
        assert len(rec.events) == 1
        e = rec.events[0]
        assert str(e.progress.reference.task_id) == "bg_1"
        assert (e.stage, e.status, e.detail) == ("split", "running", "hello")
        await pool.aclose()
        await telemetry.aclose()
