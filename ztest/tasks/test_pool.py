#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`metagpt.tasks.pool.BackgroundTaskPool`.

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

from metagpt.common.const.tasks import MAX_RESULT_LEN
from metagpt.executor.tasks import BackgroundTaskPool, BackgroundTaskNotification, BgStatus, TaskType
from metagpt.common.schema import MessagePriority

from .conftest import boom, echo, forever, gated, started_gated, wait_started


async def _drain(pool):
    """Cancel every pending task and wait for the pool to empty."""
    for tid in pool.pending_ids:
        pool.cancel(tid)
    await pool.wait_all()


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
        assert meta.status == BgStatus.PENDING
        await _drain(pool)

    def test_get_task_info_unknown(self, pool):
        assert pool.get_task_info("nope") is None


class TestCompletionPaths:
    @pytest.mark.asyncio
    async def test_success(self, pool, msg_buffer):
        tid = pool.submit(lambda: echo("hello"), "echo")
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS
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
    async def test_result_truncated(self, pool):
        big = "x" * (MAX_RESULT_LEN + 500)
        tid = pool.submit(lambda: echo(big), "big")
        await pool.wait_all()
        result = pool.get_task_info(tid).result
        assert result.endswith("...(truncated)")
        assert len(result) == MAX_RESULT_LEN + len("...(truncated)")

    @pytest.mark.asyncio
    async def test_failure_records_error_report(self, pool):
        tid = pool.submit(lambda: boom(ValueError("kaboom")), "fail")
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        # Result is the uniform <error> block; the typed report is on meta.error.
        assert meta.result.startswith("<error ")
        assert "kaboom" in meta.result
        assert meta.error is not None
        assert meta.error["error"] == "ValueError"
        assert meta.error["code"] == "UNKNOWN"
        assert meta.error["message"] == "kaboom"

    @pytest.mark.asyncio
    async def test_timeout(self, pool):
        tid = pool.submit(lambda: forever(), "hang", timeout=0.02)
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BgStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_carries_structured_error(self, pool):
        # Timeout now routes through the shared ErrorReport contract: the
        # uniform <error> block lands in the result and the machine-readable
        # report rides on meta.error (it was a bypass before — error was None).
        tid = pool.submit(lambda: forever(), "hang", timeout=0.02)
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.TIMEOUT
        assert meta.result.startswith("<error ")
        assert 'code="BG_TASK_TIMEOUT"' in meta.result
        assert meta.error is not None
        assert meta.error["code"] == "BG_TASK_TIMEOUT"
        # Timeout is retryable.
        assert 'retryable="true"' in meta.result

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, pool):
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "g", timeout=None)
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BgStatus.CANCELLED

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
        assert meta.status == BgStatus.CANCELLED
        assert meta.result.startswith("<error ")
        assert 'code="BG_TASK_CANCELLED"' in meta.result
        assert meta.error is not None
        assert meta.error["code"] == "BG_TASK_CANCELLED"
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
        assert notif.status == BgStatus.SUCCESS
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
        from metagpt.executor.tasks.disk_output import TaskOutputStore

        return BackgroundTaskPool(msg_buffer, output_store=TaskOutputStore(tmp_path))

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
        assert terminals[0].status == BgStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_progress_task_cancel_still_delivers_terminal(self, msg_buffer, tmp_path):
        pool = self._progress_pool(msg_buffer, tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(
            lambda: started_gated(started, release), "g", timeout=None, progress=True
        )
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        terminals = [
            m
            for m in msg_buffer.pop_all()
            if isinstance(m, BackgroundTaskNotification) and m.task_terminal
        ]
        assert len(terminals) == 1
        assert terminals[0].status == BgStatus.CANCELLED


class TestDeliver:
    """The single push+wake choke point all notification producers route through."""

    def test_deliver_pushes_next_and_wakes(self, msg_buffer):
        wakes = []
        pool = BackgroundTaskPool(msg_buffer, wake=lambda: wakes.append(1))
        notif = BackgroundTaskNotification(content="hi", task_id="bg_1", status=BgStatus.SUCCESS)
        pool.deliver(notif)
        # Pushed at NEXT priority and the runtime was woken.
        assert msg_buffer.pop(max_priority=MessagePriority.NOW) is None
        assert msg_buffer.pop(max_priority=MessagePriority.NEXT) is notif
        assert wakes == [1]

    def test_deliver_without_wake_still_pushes(self, msg_buffer):
        pool = BackgroundTaskPool(msg_buffer)  # no wake bound
        notif = BackgroundTaskNotification(content="hi", task_id="bg_1", status=BgStatus.FAILED)
        pool.deliver(notif)  # must not raise
        assert msg_buffer.pop_all() == [notif]

    def test_deliver_swallows_push_failure(self):
        class _BadSink:
            def push(self, *a, **k):
                raise RuntimeError("queue is gone")

        wakes = []
        pool = BackgroundTaskPool(_BadSink(), wake=lambda: wakes.append(1))
        # A delivery failure must never break the pipeline — and must not wake.
        pool.deliver(BackgroundTaskNotification(content="x"))
        assert wakes == []

    def test_deliver_is_stateless_no_terminal_dedup(self, msg_buffer):
        # deliver no longer dedups terminals: there is exactly one terminal
        # producer (_on_done), so two terminals for the same task can only occur
        # in tests. Both are pushed — deliver carries no per-task state.
        pool = BackgroundTaskPool(msg_buffer)
        first = BackgroundTaskNotification(
            content="t1", task_id="bg_1", status=BgStatus.SUCCESS, task_terminal=True
        )
        second = BackgroundTaskNotification(
            content="t2", task_id="bg_1", status=BgStatus.SUCCESS, task_terminal=True
        )
        pool.deliver(first)
        pool.deliver(second)
        assert msg_buffer.pop_all() == [first, second]

    def test_deliver_does_not_dedup_node_events(self, msg_buffer):
        # Non-terminal (node-level) events with the same task_id are never
        # deduped — a node fail then a graph success must both land.
        pool = BackgroundTaskPool(msg_buffer)
        node_a = BackgroundTaskNotification(content="node-a", task_id="bg_1", status=BgStatus.FAILED)
        node_b = BackgroundTaskNotification(content="node-b", task_id="bg_1", status=BgStatus.SUCCESS)
        pool.deliver(node_a)
        pool.deliver(node_b)
        assert msg_buffer.pop_all() == [node_a, node_b]

    def test_deliver_swallows_push_failure_and_recovers(self):
        # A swallowed push failure must not break delivery: a later push for the
        # same task still gets through (deliver is stateless, so a failed push
        # leaves no residue that would suppress a subsequent one).
        class _FlakySink:
            def __init__(self):
                self.calls = 0
                self.pushed = []

            def push(self, msg, priority=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                self.pushed.append(msg)

        sink = _FlakySink()
        pool = BackgroundTaskPool(sink)
        a = BackgroundTaskNotification(content="a", task_id="bg_1", status=BgStatus.SUCCESS, task_terminal=True)
        b = BackgroundTaskNotification(content="b", task_id="bg_1", status=BgStatus.SUCCESS, task_terminal=True)
        pool.deliver(a)  # push raises → swallowed
        pool.deliver(b)  # retry succeeds
        assert sink.pushed == [b]


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
        from metagpt.common.schema import UserMessage

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
        assert pool.get_task_info(a).status == BgStatus.RUNNING
        assert pool.get_task_info(b).status == BgStatus.PENDING
        assert started_b.is_set() is False

        release_a.set()
        await wait_started(started_b)
        assert pool.get_task_info(b).status == BgStatus.RUNNING
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
        assert meta.status == BgStatus.RUNNING  # already executing, no PENDING phase
        assert meta.command_name == "adopted"
        ev.set()
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS
        assert meta.result == "adopted-out"


class TestCapCancel:
    @pytest.mark.asyncio
    async def test_cancel_for_cap_marks_and_reports(self, pool):
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(lambda: started_gated(started, release), "huge", timeout=None)
        await wait_started(started)
        assert pool.cancel_for_cap(tid) is True
        assert pool.get_task_info(tid)._output_capped is True
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.CANCELLED
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
            if all(pool.get_task_info(t).status == BgStatus.RUNNING for t in ("bg_1", "bg_2", "bg_3")):
                break
            await asyncio.sleep(0)

        assert {m.task_id for m in pool.list_tasks_for_agent("A")} == {"bg_1", "bg_2"}
        assert {m.task_id for m in pool.list_tasks_for_agent("B")} == {"bg_3"}

        cancelled = pool.cancel_tasks_for_agent("A")
        assert set(cancelled) == {"bg_1", "bg_2"}
        ev.set()
        await pool.wait_all()
        assert pool.get_task_info("bg_1").status == BgStatus.CANCELLED
        assert pool.get_task_info("bg_3").status == BgStatus.SUCCESS

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


class TestProgressBusVisibility:
    """Pin down the contextvar visibility: a progress event reported inside a
    submitted task is mirrored onto the bus bound when the task was created."""

    @pytest.mark.asyncio
    async def test_report_progress_reaches_bus_subscriber(self, msg_buffer, tmp_path):
        from metagpt.common.events import EventBus, TaskProgressEvent, set_bus
        from metagpt.executor.tasks import TaskOutputStore
        from metagpt.executor.tasks.bggraph.report import report_progress

        class _Recorder:
            priority = 50

            def __init__(self):
                self.events = []

            def handle_sync(self, event):
                if isinstance(event, TaskProgressEvent):
                    self.events.append(event)

            async def handle(self, event):
                return None

        bus = EventBus()
        rec = _Recorder()
        bus.subscribe(rec)

        pool = BackgroundTaskPool(msg_buffer, output_store=TaskOutputStore(base_dir=tmp_path))

        async def reporter():
            report_progress("split", "running", "hello")
            return "ok"

        # ``submit`` -> ``create_task`` snapshots the active-bus contextvar, so
        # the progress emit inside the task lands on this bus.
        with set_bus(bus):
            tid = pool.submit(lambda: reporter(), "rep", progress=True)
            await pool.wait_all()

        assert tid == "bg_1"
        assert len(rec.events) == 1
        e = rec.events[0]
        assert (e.task_id, e.stage, e.status, e.detail) == ("bg_1", "split", "running", "hello")
