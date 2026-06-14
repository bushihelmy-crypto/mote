#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`metagpt.tasks.disk_output`.

Covers :class:`DiskTaskOutput` (async drain loop, incremental ``get_delta`` /
``get_tail`` reads, byte accounting, the no-event-loop sync-flush fallback,
post-close / post-cap no-ops, the disk cap + ``on_cap`` callback, and
``cleanup``) and the :class:`TaskOutputStore` registry (init / duplicate guard /
path lookup / unknown-id ``KeyError`` / ``set_on_cap`` propagation / evict vs
cleanup).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from metagpt.tasks import DiskTaskOutput, TaskOutputStore


class TestDiskTaskOutputAsync:
    @pytest.mark.asyncio
    async def test_append_and_read(self, tmp_path):
        out = DiskTaskOutput("t1", tmp_path)
        out.append("hello ")
        out.append("world")
        await out.close()
        assert out.get_size() == 11
        assert await out.get_tail() == b"hello world"

    @pytest.mark.asyncio
    async def test_get_delta_incremental(self, tmp_path):
        out = DiskTaskOutput("t2", tmp_path)
        out.append("abcde")
        await out.close()
        chunk, off = await out.get_delta(0)
        assert chunk == b"abcde"
        assert off == 5
        # Reading from the new offset yields nothing.
        chunk2, off2 = await out.get_delta(off)
        assert chunk2 == b""
        assert off2 == 5

    @pytest.mark.asyncio
    async def test_file_created_under_task_outputs(self, tmp_path):
        out = DiskTaskOutput("t3", tmp_path)
        p = Path(out.file_path)
        assert p.exists()
        assert p.parent.name == ".task_outputs"
        assert p.name == "t3.output"
        await out.close()

    @pytest.mark.asyncio
    async def test_append_after_close_is_noop(self, tmp_path):
        out = DiskTaskOutput("t4", tmp_path)
        out.append("kept")
        await out.close()
        out.append("dropped")
        assert await out.get_tail() == b"kept"

    @pytest.mark.asyncio
    async def test_close_idempotent(self, tmp_path):
        out = DiskTaskOutput("t5", tmp_path)
        out.append("x")
        await out.close()
        await out.close()  # second close must not raise
        assert await out.get_tail() == b"x"

    @pytest.mark.asyncio
    async def test_cleanup_removes_file(self, tmp_path):
        out = DiskTaskOutput("t6", tmp_path)
        out.append("data")
        await out.close()
        path = Path(out.file_path)
        assert path.exists()
        out.cleanup()
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_cap_truncates_and_fires_callback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("metagpt.tasks.disk_output.MAX_TASK_OUTPUT_BYTES", 5)
        capped_ids = []
        out = DiskTaskOutput("c1", tmp_path, on_cap=capped_ids.append)
        out.append("0123456789")  # 10 bytes > 5 cap
        await out.close()
        assert out._capped is True
        assert capped_ids == ["c1"]
        # Only the first 5 payload bytes count toward the running total.
        assert out.get_size() == 5
        # Appends after the cap are dropped.
        out.append("more")
        assert out.get_size() == 5


class TestDiskTaskOutputSync:
    def test_sync_flush_without_event_loop(self, tmp_path):
        # No running loop -> append() flushes synchronously.
        out = DiskTaskOutput("s1", tmp_path)
        out.append("abc")
        out.append("def")
        assert out.get_size() == 6
        assert Path(out.file_path).read_bytes() == b"abcdef"


class TestTaskOutputStore:
    @pytest.mark.asyncio
    async def test_init_and_read_roundtrip(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        out = store.init_output("a")
        assert isinstance(out, DiskTaskOutput)
        store.append("a", "payload")
        await store.evict("a")  # close drain to guarantee flush
        # Re-register is impossible after evict, so read via a fresh output is N/A;
        # instead assert size was tracked before evict via the DiskTaskOutput.
        assert out.get_size() == 7

    @pytest.mark.asyncio
    async def test_get_tail_and_delta_via_store(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        out = store.init_output("b")
        store.append("b", "hello")
        await out.close()  # flush
        assert await store.get_tail("b") == b"hello"
        chunk, off = await store.get_delta("b", 0)
        assert chunk == b"hello"
        assert off == 5
        assert store.get_size("b") == 5

    def test_duplicate_init_raises(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        store.init_output("dup")
        with pytest.raises(ValueError, match="already exists"):
            store.init_output("dup")

    def test_get_output_path(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        store.init_output("p")
        assert store.get_output_path("p").endswith("p.output")
        assert store.get_output_path("missing") is None

    def test_unknown_id_raises_keyerror(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        with pytest.raises(KeyError, match="Unknown task_id"):
            store.append("ghost", "x")

    @pytest.mark.asyncio
    async def test_set_on_cap_propagates(self, tmp_path, monkeypatch):
        monkeypatch.setattr("metagpt.tasks.disk_output.MAX_TASK_OUTPUT_BYTES", 3)
        capped = []
        store = TaskOutputStore(tmp_path)
        store.set_on_cap(capped.append)
        store.init_output("cap")
        store.append("cap", "abcdef")
        await store.evict("cap")
        assert capped == ["cap"]

    @pytest.mark.asyncio
    async def test_evict_keeps_file_cleanup_removes(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        out = store.init_output("e")
        store.append("e", "data")
        path = Path(out.file_path)
        await store.evict("e")
        assert path.exists()  # evict keeps the disk file
        # Evicted -> store no longer knows it.
        with pytest.raises(KeyError):
            store.get_size("e")

    @pytest.mark.asyncio
    async def test_cleanup_removes_file(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        out = store.init_output("c")
        store.append("c", "data")
        path = Path(out.file_path)
        await store.cleanup("c")
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_cleanup_all(self, tmp_path):
        store = TaskOutputStore(tmp_path)
        o1 = store.init_output("x")
        o2 = store.init_output("y")
        store.append("x", "1")
        store.append("y", "2")
        await store.cleanup_all()
        assert not Path(o1.file_path).exists()
        assert not Path(o2.file_path).exists()
