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
        a = pool.submit(forever(), "a", timeout=None)
        b = pool.submit(forever(), "b", timeout=None)
        assert a == "bg_1"
        assert b == "bg_2"
        assert pool.has_pending() is True
        assert pool.pending_count == 2
        assert set(pool.pending_ids) == {"bg_1", "bg_2"}
        await _drain(pool)

    @pytest.mark.asyncio
    async def test_meta_recorded_on_submit(self, pool):
        tid = pool.submit(forever(), "do-thing", timeout=None, task_kind="bash", agent_id="A1")
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
        tid = pool.submit(echo("hello"), "echo")
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
        tid = pool.submit(echo(None), "noop")
        await pool.wait_all()
        assert pool.get_task_info(tid).result == "(no output)"

    @pytest.mark.asyncio
    async def test_result_truncated(self, pool):
        big = "x" * (MAX_RESULT_LEN + 500)
        tid = pool.submit(echo(big), "big")
        await pool.wait_all()
        result = pool.get_task_info(tid).result
        assert result.endswith("...(truncated)")
        assert len(result) == MAX_RESULT_LEN + len("...(truncated)")

    @pytest.mark.asyncio
    async def test_failure_records_traceback(self, pool):
        tid = pool.submit(boom(ValueError("kaboom")), "fail")
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        assert "ValueError" in meta.result
        assert "kaboom" in meta.result

    @pytest.mark.asyncio
    async def test_timeout(self, pool):
        tid = pool.submit(forever(), "hang", timeout=0.02)
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BgStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, pool):
        started, release = asyncio.Event(), asyncio.Event()
        tid = pool.submit(started_gated(started, release), "g", timeout=None)
        await wait_started(started)
        assert pool.cancel(tid) is True
        await pool.wait_all()
        assert pool.get_task_info(tid).status == BgStatus.CANCELLED

    def test_cancel_unknown_returns_false(self, pool):
        assert pool.cancel("nope") is False


class TestNotification:
    @pytest.mark.asyncio
    async def test_success_notification_pushed(self, pool, msg_buffer):
        tid = pool.submit(echo("R"), "cmd")
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
        pool.submit(echo("R"), "cmd")
        await pool.wait_all()
        # NOW-priority pop should not surface a NEXT notification.
        assert msg_buffer.pop(max_priority=MessagePriority.NOW) is None
        assert msg_buffer.pop(max_priority=MessagePriority.NEXT) is not None


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
        pool.submit(gated(ev, "v"), "g", timeout=None)
        waiter = asyncio.create_task(pool.wait_all())
        await asyncio.sleep(0)
        assert not waiter.done()
        ev.set()
        await asyncio.wait_for(waiter, timeout=1)
        assert pool.has_pending() is False

    @pytest.mark.asyncio
    async def test_wait_for_completion_true(self, pool):
        ev = asyncio.Event()
        pool.submit(gated(ev, "v"), "g", timeout=None)
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
        pool.submit(gated(ev, "v"), "g", timeout=None)
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
        a = pool.submit(started_gated(started_a, release_a), "a", timeout=None)
        b = pool.submit(started_gated(started_b, release_b), "b", timeout=None)

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
        tid = pool.submit(started_gated(started, release), "huge", timeout=None)
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
        pool.submit(gated(ev), "a1", timeout=None, agent_id="A")
        pool.submit(gated(ev), "a2", timeout=None, agent_id="A")
        pool.submit(gated(ev), "b1", timeout=None, agent_id="B")
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
        pool.submit(echo(), "a")
        pool.submit(echo(), "b")
        await pool.wait_all()
        assert {m.task_id for m in pool.list_tasks()} == {"bg_1", "bg_2"}


class TestTaskTypeDefault:
    @pytest.mark.asyncio
    async def test_default_task_type_is_coroutine(self, pool):
        tid = pool.submit(forever(), "c", timeout=None)
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
            tid = pool.submit(reporter(), "rep", progress=True)
            await pool.wait_all()

        assert tid == "bg_1"
        assert len(rec.events) == 1
        e = rec.events[0]
        assert (e.task_id, e.stage, e.status, e.detail) == ("bg_1", "split", "running", "hello")
