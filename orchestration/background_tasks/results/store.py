"""Disk-backed output writer for background tasks with incremental reads.

Each task gets a file on disk;
a per-task async drain loop batches a write queue so append callers stay
non-blocking. Consumers read incrementally via ``get_delta(from_offset)`` or grab the
tail with ``get_tail(max_bytes)``.

This module is standalone — it does **not** modify ``BackgroundTaskPool`` or
the owner-private task state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional, Union

from mote.contracts.ports.task.operations import TaskOutputLocationPort
from mote.contracts.session.identity import SessionId
from mote.contracts.task.models import TaskId
from mote.orchestration.background_tasks.constants import (
    DEFAULT_MAX_READ_BYTES,
    MAX_TASK_OUTPUT_BYTES,
    MAX_TASK_OUTPUT_BYTES_DISPLAY,
)
from mote.runtime.persistence import disk_io
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.telemetry.logging import logger

# Sentinel object to signal drain loop shutdown (never confused with real data).
_SENTINEL = object()


class DiskTaskOutput:
    """Per-task disk-backed output writer with async drain loop.

    The drain loop is started lazily on the first :meth:`append` call so that
    construction works in both sync and async contexts.
    """

    def __init__(
        self,
        task_id: TaskId,
        output_dir: Union[str, Path],
        on_cap: Optional[Callable[[TaskId], None]] = None,
    ) -> None:
        self.task_id = task_id
        # *output_dir* is the already-resolved ``task_outputs/`` space for this
        # task's session (from :class:`SessionWorkspace`); this class holds no
        # layout knowledge of its own.
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)
        self._file_path = base / f"{task_id}.output"
        # Create (or truncate) the output file
        disk_io.truncate_file(self._file_path)

        self._queue: asyncio.Queue = asyncio.Queue()
        self._bytes_written: int = 0
        self._capped: bool = False
        self._closed: bool = False
        self._drain_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._flushed = asyncio.Event()
        self._flushed.set()
        self._on_cap = on_cap

    @property
    def file_path(self) -> str:
        """Absolute path of the output file on disk."""
        return str(self._file_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, data: Union[bytes, str]) -> None:
        """Enqueue *data* for async writing. Non-blocking."""
        if self._closed or self._capped:
            return
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._flushed.clear()
        self._queue.put_nowait(data)
        self._ensure_drain()

    async def get_delta(
        self,
        from_offset: int,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> tuple[bytes, int]:
        """Read from *from_offset* up to *max_bytes*.

        Returns ``(content, new_offset)`` so the caller can pass
        ``new_offset`` back on the next call for incremental reads.
        """
        content = await run_disk_io(disk_io.read_range, self._file_path, from_offset, max_bytes)
        return content, from_offset + len(content)

    async def get_tail(self, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> bytes:
        """Read the last *max_bytes* of the output."""
        return await run_disk_io(disk_io.read_tail, self._file_path, max_bytes)

    def get_size(self) -> int:
        """Return the number of bytes written so far."""
        return self._bytes_written

    async def close(self) -> None:
        """Drain remaining queued data and stop the drain loop."""
        if self._closed:
            return
        self._closed = True
        if self._drain_task is not None:
            # Push a sentinel object to unblock the drain loop
            self._queue.put_nowait(_SENTINEL)
            await self._drain_task
        # else: drain loop was never started — nothing to flush

    async def flush(self) -> None:
        """Wait until all output accepted before this call is written."""
        if self._flushed.is_set():
            return
        drain = self._drain_task
        if drain is None:
            self._sync_flush()
            self._flushed.set()
            return
        waiter = asyncio.create_task(self._flushed.wait())
        done, _ = await asyncio.wait(
            {waiter, drain},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if drain in done:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await drain
        await waiter

    def cleanup(self) -> None:
        """Delete the disk file."""
        try:
            disk_io.remove_file(self._file_path)
        except OSError as e:
            logger.warning(f"Failed to remove task output file {self._file_path}: {e}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_drain(self) -> None:
        """Start the drain loop task if it hasn't been started yet.

        Falls back to synchronous write when no event loop is running.
        """
        if self._drain_task is None:
            try:
                loop = asyncio.get_running_loop()
                self._drain_task = loop.create_task(self._drain_loop())
            except RuntimeError:
                # No running event loop — flush synchronously
                self._sync_flush()

    def _sync_flush(self) -> None:
        """Drain the queue synchronously (fallback when no event loop)."""
        chunks: list[bytes] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not _SENTINEL:
                chunks.append(item)
        if chunks:
            payload = b"".join(chunks)
            disk_io.write_bytes(self._file_path, payload, append=True)
            self._bytes_written += len(payload)

    async def _drain_loop(self) -> None:
        """Consume the write queue and batch-append to disk."""
        while True:
            item = await self._queue.get()

            if item is _SENTINEL:
                # Drain any remaining real data before exiting
                chunks = self._collect_remaining()
                if chunks:
                    await self._write_chunks(chunks)
                break

            chunks = [item]
            # Batch: grab all immediately available items
            shutdown = False
            while not self._queue.empty():
                c = self._queue.get_nowait()
                if c is _SENTINEL:
                    shutdown = True
                    break
                chunks.append(c)

            await self._write_chunks(chunks)

            if shutdown:
                # Drain anything left after the sentinel
                remaining = self._collect_remaining()
                if remaining:
                    await self._write_chunks(remaining)
                break

    def _collect_remaining(self) -> list[bytes]:
        """Collect all remaining real data items from the queue."""
        chunks: list[bytes] = []
        while not self._queue.empty():
            c = self._queue.get_nowait()
            if c is not _SENTINEL:
                chunks.append(c)
        return chunks

    async def _write_chunks(self, chunks: list[bytes]) -> None:
        """Write a batch of chunks to disk, respecting the cap."""
        payload = b"".join(chunks)
        if not payload:
            return

        cap_notice = (
            f"\n[output truncated: exceeded {MAX_TASK_OUTPUT_BYTES_DISPLAY} disk cap]\n".encode()
            if self._bytes_written + len(payload) > MAX_TASK_OUTPUT_BYTES
            else b""
        )
        async with self._write_lock:
            written_data, capped = await run_disk_io(
                disk_io.write_capped,
                self._file_path,
                payload,
                MAX_TASK_OUTPUT_BYTES,
                current_size=self._bytes_written,
                append=True,
                cap_notice=cap_notice,
            )
        self._bytes_written += written_data
        if self._queue.empty():
            self._flushed.set()

        if capped:
            self._capped = True
            # Notify the owner (e.g. BackgroundTaskPool) so it can kill the
            # source task via the output watchdog.
            if self._on_cap is not None:
                try:
                    self._on_cap(self.task_id)
                except Exception as e:
                    logger.warning(f"on_cap callback failed for task {self.task_id}: {e}")


class TaskOutputStore:
    """Registry of per-task disk outputs.

    Files are stored in the ``task_outputs/`` space under the session directory
    (``{root}/.agent_sessions/{session_id}/task_outputs/{task_id}.output``),
    resolved through the shared :class:`SessionWorkspace`. Both of a task's on-disk
    artifacts (this stdout log and its large-result overflow) therefore live
    under one session tree and are swept together with the session.

    Accepts an optional ``base_dir`` (a workspace *root*) for convenience — it is
    wrapped in a :class:`SessionWorkspace` — or, for the injected single-owner
    path, an explicit ``store``. Defaults to the standard workspace root.
    """

    def __init__(
        self,
        base_dir: Union[str, Path, None] = None,
        *,
        session_id: SessionId,
        store: Optional[TaskOutputLocationPort] = None,
    ) -> None:
        self._store = store if store is not None else SessionWorkspace(base_dir)
        self._session_id = session_id
        self._outputs: dict[TaskId, DiskTaskOutput] = {}
        self._on_cap: Optional[Callable[[TaskId], None]] = None

    @property
    def store(self) -> TaskOutputLocationPort:
        """The workspace layout owner backing this store.

        The pool reuses it to scope a large whole-task *result* file into the
        same session tree as this streaming stdout log, so both persisted
        artifacts share one owner and one layout.
        """
        return self._store

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    def set_on_cap(self, callback: Callable[[TaskId], None]) -> None:
        """Set a callback invoked when any task's output hits the disk cap.

        The callback receives the ``task_id``.  Typical usage::

            store.set_on_cap(lambda tid: bg_pool.cancel_for_cap(tid))
        """
        self._on_cap = callback

    def init_output(self, task_id: TaskId) -> DiskTaskOutput:
        """Create and register a new task output."""
        if task_id in self._outputs:
            raise ValueError(f"Task output already exists: {task_id}")
        output_dir = self._store.output_directory(self._session_id)
        output = DiskTaskOutput(task_id, output_dir, on_cap=self._on_cap)
        self._outputs[task_id] = output
        return output

    def get_output_path(self, task_id: TaskId) -> str | None:
        """Return the disk path for a task's output file, or None if unknown."""
        output = self._outputs.get(task_id)
        return output.file_path if output is not None else None

    def _get(self, task_id: TaskId) -> DiskTaskOutput:
        try:
            return self._outputs[task_id]
        except KeyError:
            raise KeyError(f"Unknown task_id: {task_id}")

    def append(self, task_id: TaskId, data: Union[bytes, str]) -> None:
        """Append data to a task's output."""
        self._get(task_id).append(data)

    async def get_delta(
        self,
        task_id: TaskId,
        from_offset: int,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> tuple[bytes, int]:
        """Incremental read from a task's output."""
        return await self._get(task_id).get_delta(from_offset, max_bytes)

    async def get_tail(
        self,
        task_id: TaskId,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> bytes:
        """Tail read from a task's output."""
        return await self._get(task_id).get_tail(max_bytes)

    async def flush(self, task_id: TaskId) -> None:
        """Settle all output accepted for one task without closing its stream."""
        output = self._outputs.get(task_id)
        if output is not None:
            await output.flush()

    def get_size(self, task_id: TaskId) -> int:
        """Get the output size for a task."""
        return self._get(task_id).get_size()

    async def evict(self, task_id: TaskId) -> None:
        """Close the drain loop but keep the disk file."""
        output = self._outputs.pop(task_id, None)
        if output is not None:
            await output.close()

    async def cleanup(self, task_id: TaskId) -> None:
        """Delete both in-memory state and disk file for a task."""
        output = self._outputs.pop(task_id, None)
        if output is not None:
            await output.close()
            output.cleanup()

    async def cleanup_all(self) -> None:
        """Clean up all tasks."""
        for task_id in list(self._outputs):
            await self.cleanup(task_id)

    async def aclose(self) -> None:
        """Flush every task stream while preserving its durable output file."""

        for task_id in list(self._outputs):
            await self.evict(task_id)
