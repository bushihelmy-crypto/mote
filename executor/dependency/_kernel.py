"""Persistent Python-kernel engine — the backend for the single ``python`` tool.

The Python sibling of the persistent ``terminal``: instead of a PTY-backed shell
this keeps **one live Jupyter (ipykernel) process per Role session** that the
model executes code into across calls — exactly like a notebook kernel:

  * top-level variables, imports, and defined functions persist between calls;
  * each call blocks until the kernel returns to idle (the execution finished) or
    the timeout elapses, in which case the kernel is interrupted and whatever it
    printed so far is returned (its state is preserved);
  * ``interrupt`` sends a ``KeyboardInterrupt`` to a wedged kernel, ``restart``
    rebuilds a clean kernel (clears all state), ``close`` shuts it down.

Output is collected off the kernel's iopub channel: ``stream`` (stdout/stderr),
``execute_result`` / ``display_data`` (the repr of the last expression), and
``error`` (the traceback, ANSI-stripped) are concatenated; the ``status: idle``
message marks the end of an execution.

The live :class:`KernelSession` is owned by the Role: the ``Python`` tool stores
it on the Role's ``RoleState`` (one implicit kernel per session, like the
``terminal`` tool — there is no model-facing kernel id) rather than in a
process-global registry, so kernels are isolated per Role and torn down with it.
This module owns only the engine; the per-Role lifecycle lives in the tool.
"""
from __future__ import annotations

import asyncio
import queue
import re
import signal
from typing import Optional

from metagpt.executor.tool_result import ToolError

# --- Constants -------------------------------------------------------------
# Default execute timeout: how long a call blocks for the kernel to return to
# idle before interrupting and yielding partial output.
DEFAULT_TIMEOUT_S = 60.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 600.0
# Grace window to drain output (KeyboardInterrupt traceback + idle) after an
# interrupt is sent.
_INTERRUPT_GRACE_S = 5.0
# Kernel start / restart readiness timeout.
_READY_TIMEOUT_S = 30.0
# Output cap: keep a head 50% + tail 50%, drop the middle.
OUTPUT_MAX_CHARS = 1024 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences (kernel tracebacks are colourised)."""
    return _ANSI_RE.sub("", text)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _cap_text(text: str) -> str:
    """Cap *text* at :data:`OUTPUT_MAX_CHARS`, keeping head + tail, drop middle."""
    if len(text) <= OUTPUT_MAX_CHARS:
        return text
    head = OUTPUT_MAX_CHARS // 2
    tail = OUTPUT_MAX_CHARS - head
    omitted = len(text) - OUTPUT_MAX_CHARS
    return f"{text[:head]}\n[... {omitted} chars omitted ...]\n{text[-tail:]}"


class KernelSession:
    """One persistent ipykernel process owned by a Role session.

    Wraps a :class:`jupyter_client.AsyncKernelManager` + client. :meth:`execute`
    sends code on the shell channel and drains the iopub channel until the kernel
    returns to idle (or the deadline passes, after which the kernel is
    interrupted and the partial output returned).
    """

    def __init__(self, *, session_key: str, cwd: Optional[str]) -> None:
        self.session_key = session_key
        self.cwd = cwd
        self._km = None  # AsyncKernelManager
        self._kc = None  # AsyncKernelClient
        self._closed = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        from jupyter_client.manager import AsyncKernelManager

        self._km = AsyncKernelManager()
        await self._km.start_kernel(cwd=self.cwd)
        self._kc = self._km.client()
        self._kc.start_channels()
        try:
            await self._kc.wait_for_ready(timeout=_READY_TIMEOUT_S)
        except RuntimeError as e:
            self.kill()
            raise ToolError(f"Error: Python kernel failed to start: {e}")

    @property
    def closed(self) -> bool:
        return self._closed or self._km is None

    # --- output plumbing ---------------------------------------------------

    @staticmethod
    def _accumulate(msg: dict, parts: list[str]) -> bool:
        """Append a single iopub message's payload to *parts*.

        Returns True when this is the terminal ``status: idle`` message.
        """
        msg_type = msg["msg_type"]
        content = msg["content"]
        if msg_type == "stream":
            parts.append(content.get("text", ""))
        elif msg_type in ("execute_result", "display_data"):
            data = content.get("data", {})
            text = data.get("text/plain")
            if text:
                parts.append(text if text.endswith("\n") else text + "\n")
        elif msg_type == "error":
            parts.append(_strip_ansi("\n".join(content.get("traceback", []))) + "\n")
        elif msg_type == "status" and content.get("execution_state") == "idle":
            return True
        return False

    async def _drain(
        self, msg_id: str, parts: list[str], deadline: float
    ) -> bool:
        """Drain iopub messages for *msg_id* until idle or *deadline*.

        Returns True if idle was reached, False on timeout.
        """
        loop = asyncio.get_event_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                msg = await self._kc.get_iopub_msg(timeout=remaining)
            except queue.Empty:
                return False
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            if self._accumulate(msg, parts):
                return True

    # --- operations --------------------------------------------------------

    async def execute(self, code: str, timeout: float) -> tuple[str, bool]:
        """Run *code*, block until idle or *timeout*; return (text, timed_out).

        On timeout the kernel is interrupted (its state is preserved) and a short
        grace window drains the KeyboardInterrupt traceback + idle marker.
        """
        timeout = _clamp(timeout, MIN_TIMEOUT_S, MAX_TIMEOUT_S)
        msg_id = self._kc.execute(code)
        loop = asyncio.get_event_loop()
        parts: list[str] = []
        idle = await self._drain(msg_id, parts, loop.time() + timeout)
        if idle:
            return _cap_text("".join(parts)), False
        # Timed out: interrupt and drain the aftermath for a short grace window.
        await self._km.interrupt_kernel()
        await self._drain(msg_id, parts, loop.time() + _INTERRUPT_GRACE_S)
        return _cap_text("".join(parts)), True

    async def interrupt(self) -> str:
        """Send a KeyboardInterrupt to the kernel and drain any aftermath."""
        await self._km.interrupt_kernel()
        loop = asyncio.get_event_loop()
        parts: list[str] = []
        await self._drain("", parts, loop.time() + _INTERRUPT_GRACE_S)
        return _cap_text("".join(parts))

    async def restart(self) -> None:
        """Restart the kernel — clears all in-memory state."""
        await self._km.restart_kernel(now=True)
        try:
            await self._kc.wait_for_ready(timeout=_READY_TIMEOUT_S)
        except RuntimeError as e:
            self.kill()
            raise ToolError(f"Error: Python kernel failed to restart: {e}")

    async def shutdown(self) -> None:
        """Graceful async teardown."""
        self._closed = True
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception:  # noqa: BLE001
                pass
        if self._km is not None:
            try:
                await self._km.shutdown_kernel(now=True)
            except Exception:  # noqa: BLE001
                self.kill()
        self._km = None
        self._kc = None

    def kill(self) -> None:
        """Best-effort synchronous teardown (idempotent) — for cleanup_session.

        ``shutdown_kernel`` is a coroutine; cleanup runs synchronously, so we
        SIGKILL the kernel's launched process directly.
        """
        self._closed = True
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception:  # noqa: BLE001
                pass
            self._kc = None
        if self._km is not None:
            proc = getattr(getattr(self._km, "provisioner", None), "process", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            self._km = None
