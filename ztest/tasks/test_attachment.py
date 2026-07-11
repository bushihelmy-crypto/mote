#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.tasks.attachment`.

Covers the ``format_attachment_xml`` renderer (with/without delta, escaping,
enum vs str status), and the :class:`TaskAttachmentGenerator` state machine: pending /
running (with incremental delta + offset advance) attachments, first-time
terminal final attachments vs the pool-already-notified skip, eviction of
previously-notified terminal tasks, and ``mark_notified``.
"""
from __future__ import annotations

import time

import pytest
from mote.executor.tasks import BgStatus, TaskAttachment, TaskAttachmentGenerator, TaskMeta, format_attachment_xml


class FakePool:
    def __init__(self, metas):
        self._metas = metas

    def list_tasks(self):
        return list(self._metas)


class FakeStore:
    """In-memory delta/tail source keyed by task id."""

    def __init__(self):
        self.deltas: dict[str, bytes] = {}
        self.tails: dict[str, bytes] = {}

    async def get_delta(self, task_id, from_offset, max_bytes):
        data = self.deltas.get(task_id, b"")[from_offset : from_offset + max_bytes]
        return data, from_offset + len(data)

    async def get_tail(self, task_id, max_bytes):
        return self.tails.get(task_id, b"")[-max_bytes:]


class TestFormatXml:
    def test_with_delta_and_escaping(self):
        att = TaskAttachment(
            task_id="bg_1",
            status=BgStatus.RUNNING,
            command_name="run & wait",
            description="running",
            delta_summary="line<1>",
        )
        xml = format_attachment_xml(att)
        assert "<task-id>bg_1</task-id>" in xml
        assert "<command>run &amp; wait</command>" in xml
        assert "<status>running</status>" in xml  # enum rendered as its value
        assert "<delta-summary>line&lt;1&gt;</delta-summary>" in xml
        assert xml.startswith("<task-attachment>")
        assert xml.endswith("</task-attachment>")

    def test_without_delta_omits_tag(self):
        att = TaskAttachment(
            task_id="bg_1",
            status="pending",
            command_name="cmd",
            description="queued",
            delta_summary=None,
        )
        xml = format_attachment_xml(att)
        assert "<delta-summary>" not in xml
        assert "<status>pending</status>" in xml

    def test_error_renders_uniform_error_block(self):
        from mote.common.exception import ErrorReport

        report = ErrorReport.from_exception(RuntimeError("kaboom"))
        att = TaskAttachment(
            task_id="bg_1",
            status=BgStatus.FAILED,
            command_name="cmd",
            description="failed",
            delta_summary=None,
            error=report.as_dict(),
        )
        xml = format_attachment_xml(att)
        # Same <error> envelope every executor surface uses.
        assert '<error code="UNKNOWN"' in xml
        assert "kaboom" in xml
        assert "</error>" in xml

    def test_no_error_omits_error_block(self):
        att = TaskAttachment(
            task_id="bg_1",
            status=BgStatus.SUCCESS,
            command_name="cmd",
            description="done",
            delta_summary=None,
        )
        assert "<error" not in format_attachment_xml(att)


class TestGeneratePending:
    @pytest.mark.asyncio
    async def test_pending_attachment(self):
        meta = TaskMeta(task_id="bg_1", command_name="job", status=BgStatus.PENDING)
        gen = TaskAttachmentGenerator(FakePool([meta]))
        result = await gen.generate()
        assert len(result.attachments) == 1
        att = result.attachments[0]
        assert att.status == BgStatus.PENDING
        assert "is pending" in att.description
        assert att.delta_summary is None


class TestGenerateRunning:
    @pytest.mark.asyncio
    async def test_running_with_delta_then_advances_offset(self):
        meta = TaskMeta(task_id="bg_1", command_name="job", status=BgStatus.RUNNING)
        store = FakeStore()
        store.deltas["bg_1"] = b"first-chunk"
        gen = TaskAttachmentGenerator(FakePool([meta]), store)

        r1 = await gen.generate()
        assert r1.attachments[0].delta_summary == "first-chunk"
        assert "is running" in r1.attachments[0].description

        # No new bytes beyond the consumed offset -> delta_summary is None now.
        r2 = await gen.generate()
        assert r2.attachments[0].delta_summary is None

    @pytest.mark.asyncio
    async def test_running_without_store(self):
        meta = TaskMeta(task_id="bg_1", command_name="job", status=BgStatus.RUNNING)
        gen = TaskAttachmentGenerator(FakePool([meta]))
        r = await gen.generate()
        assert r.attachments[0].delta_summary is None


class TestGenerateTerminal:
    @pytest.mark.asyncio
    async def test_first_time_terminal_emits_final_attachment(self):
        meta = TaskMeta(
            task_id="bg_1",
            command_name="job",
            status=BgStatus.SUCCESS,
            end_time=time.time(),
            notified=False,  # pool did NOT push a notification
        )
        store = FakeStore()
        store.tails["bg_1"] = b"final output"
        gen = TaskAttachmentGenerator(FakePool([meta]), store)

        r1 = await gen.generate()
        assert len(r1.attachments) == 1
        att = r1.attachments[0]
        assert att.delta_summary == "final output"
        assert "success" in att.description

        # Now marked notified internally -> next round evicts it.
        r2 = await gen.generate()
        assert r2.attachments == []
        assert r2.evicted_task_ids == ["bg_1"]

    @pytest.mark.asyncio
    async def test_failed_terminal_threads_error_report(self):
        from mote.common.exception import ErrorReport

        report = ErrorReport.from_exception(RuntimeError("kaboom"))
        meta = TaskMeta(
            task_id="bg_1",
            command_name="job",
            status=BgStatus.FAILED,
            end_time=time.time(),
            notified=False,
            error=report.as_dict(),
        )
        gen = TaskAttachmentGenerator(FakePool([meta]))
        r = await gen.generate()
        att = r.attachments[0]
        assert att.error == report.as_dict()
        assert '<error code="UNKNOWN"' in format_attachment_xml(att)

    @pytest.mark.asyncio
    async def test_pool_notified_terminal_is_skipped_then_evicted(self):
        meta = TaskMeta(
            task_id="bg_1",
            command_name="job",
            status=BgStatus.SUCCESS,
            end_time=time.time(),
            notified=True,  # _on_done already pushed a notification
        )
        gen = TaskAttachmentGenerator(FakePool([meta]))

        # No duplicate attachment; just mark internally.
        r1 = await gen.generate()
        assert r1.attachments == []
        assert r1.evicted_task_ids == []

        # Subsequent round evicts.
        r2 = await gen.generate()
        assert r2.evicted_task_ids == ["bg_1"]

    @pytest.mark.asyncio
    async def test_mark_notified_then_evicts(self):
        meta = TaskMeta(
            task_id="bg_1",
            command_name="job",
            status=BgStatus.FAILED,
            end_time=time.time(),
            notified=False,
        )
        gen = TaskAttachmentGenerator(FakePool([meta]))
        gen.mark_notified("bg_1")
        r = await gen.generate()
        assert r.attachments == []
        assert r.evicted_task_ids == ["bg_1"]
