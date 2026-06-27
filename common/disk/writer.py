"""DiskWriter — a single serial async write queue (L1 of the persistence stack).

Every durable writer in the system (session rollout, residency record, cron
schedule, blob store) used to spin its own ``tmp+fsync+replace`` or
``O_APPEND+flush`` and had no shared ordering / drain discipline. DiskWriter is
the one execution layer beneath the event-bus spine that gives them:

* **per-``key`` FIFO** — writes tagged with the same key (a target file/stream)
  land in submission order. The implementation uses a single worker (a global
  total order, a legal stronger guarantee); if head-of-line blocking ever hurts,
  a sharded worker can replace it without breaking the contract.
* **a global ``drain()`` barrier** — await until the queue is empty, used as the
  durability checkpoint at turn boundaries, on shutdown, and before any in-process
  replay (resume / fork / rehydrate).

Contract:

* ``enqueue(key, fn)`` — fire-and-forget, ordered by key; a failing ``fn`` is
  logged and skipped (mirrors the bus's bad-subscriber isolation).
* ``await submit(key, fn) -> T`` — ordered + awaited to completion (``fn`` should
  fsync internally for true durability), returning ``fn``'s result or raising.
* ``await drain()`` — wait for the current backlog to flush.
* ``await aclose()`` — drain, then stop the worker (idempotent).

**sync-fallback**: when there is no running event loop (a pure-disk ``fork()``,
or a test that calls ``asyncio.run`` per call so each gets a fresh loop),
``enqueue``/``submit`` run ``fn`` inline synchronously and ``drain`` is a no-op.
This keeps the primitive usable from synchronous call sites and sidesteps the
"worker lazy-bound to the first loop then orphaned" trap.

Leaf module: imports only stdlib + ``common.logs`` (same discipline as
``common/events``). It must never import roles/context/executor/session.
"""

from __future__ import annotations

import asyncio
import atexit
from typing import Awaitable, Callable, Optional, Tuple, TypeVar

from metagpt.common.logs import logger

T = TypeVar("T")

#: A queued unit of work: ``(key, fn, future_or_None)``. ``future`` is set for
#: ``submit`` (result/exception delivered back) and ``None`` for ``enqueue``.
_Job = Tuple[str, Callable[[], object], Optional["asyncio.Future"]]


class DiskWriter:
    """A single-worker serial disk write queue with a drain barrier."""

    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Worker lifecycle (lazy: bound to the loop running when first enqueued)
    # ------------------------------------------------------------------
    def _ensure_worker(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        if self._worker is None or self._loop is not loop:
            # First use on this loop (or a brand-new loop replaced the old one):
            # start a fresh queue + worker bound to it.
            self._loop = loop
            self._queue = asyncio.Queue()
            self._closed = False
            self._worker = loop.create_task(self._run())
        assert self._queue is not None
        return self._queue

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            if job is None:  # shutdown sentinel
                self._queue.task_done()
                break
            _key, fn, future = job
            # ``fn`` runs *inline* in this coroutine, not on a worker thread: the
            # write is fully on disk before the coroutine next suspends. That is
            # what keeps the queue the single source of truth about durability —
            # a job is either still queued (the sync barrier flushes it) or done.
            # A thread pool would split that into a third "off the queue but not
            # yet on disk" state the synchronous barrier could not observe. JSONL
            # appends are tiny, so blocking the loop for one is negligible.
            try:
                result = fn()
                if future is not None and not future.done():
                    future.set_result(result)
            except Exception as exc:  # noqa: BLE001 — one bad write never breaks the queue
                if future is not None and not future.done():
                    future.set_exception(exc)
                else:
                    logger.warning(f"DiskWriter: write for key {_key!r} failed: {exc}")
            finally:
                self._queue.task_done()

    @staticmethod
    def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def enqueue(self, key: str, fn: Callable[[], object]) -> None:
        """Fire-and-forget ordered write. Runs inline when there is no loop."""
        loop = self._running_loop()
        if loop is None:
            # sync-fallback: no event loop to schedule onto, run inline.
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — match the async error isolation
                logger.warning(f"DiskWriter: inline write for key {key!r} failed: {exc}")
            return
        queue = self._ensure_worker(loop)
        queue.put_nowait((key, fn, None))

    async def submit(self, key: str, fn: Callable[[], T]) -> T:
        """Ordered write awaited to completion; returns ``fn``'s result or raises."""
        loop = self._running_loop()
        if loop is None:
            return fn()  # sync-fallback
        queue = self._ensure_worker(loop)
        future: asyncio.Future = loop.create_future()
        queue.put_nowait((key, fn, future))
        return await future

    async def drain(self) -> None:
        """Wait until the current backlog has been fully processed (barrier).

        When the worker is bound to the running loop, awaits the queue to empty.
        Otherwise (no loop, or items left over from a finished loop) flushes any
        pending jobs inline so the barrier is still honored.
        """
        loop = self._running_loop()
        if self._queue is None:
            return
        if loop is not None and self._loop is loop and self._worker is not None:
            await self._queue.join()
            return
        # The worker's loop is gone (or never started): run leftovers inline.
        self.flush_inline()

    def flush_inline(self) -> None:
        """Synchronously run every queued job, draining the queue (no loop needed).

        ``asyncio.Queue``'s non-blocking ops work without a running loop, so this
        can flush a backlog left behind when a short-lived loop (``asyncio.run``)
        finished before its worker processed everything. Used by the synchronous
        :func:`drain_blocking` barrier and the ``atexit`` safety net.

        The queue is the whole story: because the worker writes inline (never on
        a thread), a job is either still here — flushed below — or already on
        disk. There is no in-flight-on-another-thread state to wait out.
        """
        if self._queue is None:
            return
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if job is None:
                    continue
                key, fn, future = job
                try:
                    result = fn()
                    if future is not None and not future.done():
                        future.set_result(result)
                except Exception as exc:  # noqa: BLE001 — isolate one bad write
                    if future is not None and not future.done():
                        future.set_exception(exc)
                    else:
                        logger.warning(f"DiskWriter: inline flush for key {key!r} failed: {exc}")
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass  # task_done called more times than items (already accounted)

    async def aclose(self) -> None:
        """Drain then stop the worker. Idempotent; never hangs on a dead loop."""
        if self._closed:
            return
        self._closed = True
        loop = self._running_loop()
        if self._worker is not None and self._queue is not None and self._loop is loop and loop is not None:
            await self._queue.join()
            self._queue.put_nowait(None)  # shutdown sentinel
            try:
                await self._worker
            except Exception:  # noqa: BLE001
                pass
        else:
            # Worker's loop is gone / never ran: flush whatever is left inline.
            self.flush_inline()
        self._worker = None
        self._queue = None
        self._loop = None


# ----------------------------------------------------------------------
# Process-level singleton
# ----------------------------------------------------------------------
_writer: Optional[DiskWriter] = None
_atexit_registered = False


def get_disk_writer() -> DiskWriter:
    """Return the process-wide :class:`DiskWriter`, creating it on first use."""
    global _writer, _atexit_registered
    if _writer is None:
        _writer = DiskWriter()
        if not _atexit_registered:
            atexit.register(_atexit_close)
            _atexit_registered = True
    return _writer


def set_disk_writer(writer: Optional[DiskWriter]) -> None:
    """Override the singleton (tests inject an isolated writer; ``None`` resets)."""
    global _writer
    _writer = writer


def drain_blocking() -> None:
    """Synchronous durability barrier for sync call sites before an in-process replay.

    Flushes any pending jobs inline so everything queued so far is on disk before
    the caller reads it back. This covers:

    * **no running loop** — the pure sync-fallback world (queue already empty) or
      a backlog left over from a finished ``asyncio.run`` loop whose worker was
      torn down before draining; and
    * **a sync frame inside a running loop** (e.g. ``resume_session`` / ``fork``
      invoked mid-turn) — the loop is single-threaded and the worker writes
      inline, so while this frame runs the worker is necessarily suspended at
      ``queue.get`` (every dequeued job is already on disk, never mid-write on a
      thread); ``flush_inline`` removes each remaining job before running it, so
      the worker never re-runs it.

    Cross-process resume is safe regardless: the previous process drained at its
    last turn boundary / ``aclose``, so the file is complete on disk.
    """
    if _writer is not None:
        _writer.flush_inline()


def _atexit_close() -> None:
    """Best-effort flush of the singleton at interpreter shutdown.

    Runs queued jobs inline (no event loop is created, no queue ``join`` that
    could hang on a worker whose loop already closed).
    """
    if _writer is None:
        return
    try:
        _writer.flush_inline()
    except Exception:  # noqa: BLE001 — shutdown is best-effort
        pass


__all__ = ["DiskWriter", "get_disk_writer", "set_disk_writer", "drain_blocking"]
