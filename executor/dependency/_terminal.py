"""Persistent-terminal engine — the backend for the single ``terminal`` tool.

Unlike the one-shot :class:`Bash` tool (a fresh subprocess per call), this keeps
**one live shell per Role session**, PTY-backed, that the model types into across
calls — exactly like a real terminal tab:

  * ``cd`` / ``export`` / ``source venv/bin/activate`` / aliases persist;
  * typing ``python3`` puts a REPL in the foreground, and subsequent input is fed
    to *it* (the PTY routes input to whatever owns the terminal); ``exit()`` drops
    back to the shell;
  * a password / auth prompt can be answered by typing into it.

How "command done" is detected (the crux): the shell's ``PROMPT_COMMAND`` prints a
per-session nonce marker ``__TERM_<nonce>__<exit_code>__END`` every time it returns
to a prompt. So after sending input we read until either

  * the marker appears  -> the shell is idle at a prompt (command finished; we get
    its exit code), or
  * the yield window elapses without a marker -> a foreground program is still
    running and holding the terminal (e.g. ``python3``, ``npm run dev``).

This is the standard "sentinel prompt" technique used by expect-style persistent
shells. Echo is disabled on the PTY so the typed line is not duplicated in output.

The live :class:`TerminalSession` is owned by the Role: the ``Terminal`` tool
stores it on the Role's ``RoleState`` (one implicit terminal per session, like a
Jupyter kernel — there is no model-facing session id) rather than in a
process-global registry, so terminals are isolated per Role and torn down with
it. This module owns only the engine; the per-Role lifecycle lives in the tool.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import uuid
from typing import Optional

from metagpt.executor.tool_result import ToolError

# --- Constants -------------------------------------------------------------
# Default yield window: how long a call waits for output before yielding.
DEFAULT_YIELD_MS = 10_000
MIN_YIELD_MS = 250
MAX_YIELD_MS = 60_000
# Output buffer cap: 1 MiB retained (head 50% + tail 50%, middle dropped).
OUTPUT_MAX_BYTES = 1024 * 1024
# Ctrl-C (ETX).
INTERRUPT = "\x03"

_READ_CHUNK = 65_536
# How long start() waits for the shell's first prompt marker (ready signal).
_READY_TIMEOUT_S = 5.0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _decode(data: bytes) -> str:
    """Decode PTY output, normalising the terminal's CRLF line endings to LF."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


class HeadTailBuffer:
    """A byte buffer capped at ``max_bytes``: keep a head 50% + tail 50%, drop the
    middle, and remember how many bytes were omitted.

    Bytes fill the head budget first, then accumulate in a capped tail (oldest tail
    bytes dropped). ``render`` stitches the two with a ``[... N bytes omitted ...]``
    marker. The prompt marker always arrives at the very end, so it stays in the
    (preserved) tail and can be stripped from the rendered text.
    """

    def __init__(self, max_bytes: int = OUTPUT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self.head_budget = max_bytes // 2
        self.tail_budget = max_bytes - self.head_budget
        self._head = bytearray()
        self._tail = bytearray()
        self.omitted = 0

    def append(self, chunk: bytes) -> None:
        if self.max_bytes == 0:
            self.omitted += len(chunk)
            return
        if len(self._head) < self.head_budget:
            remaining = self.head_budget - len(self._head)
            if len(chunk) <= remaining:
                self._head += chunk
                return
            self._head += chunk[:remaining]
            self._push_tail(chunk[remaining:])
            return
        self._push_tail(chunk)

    def _push_tail(self, chunk: bytes) -> None:
        if self.tail_budget == 0:
            self.omitted += len(chunk)
            return
        self._tail += chunk
        excess = len(self._tail) - self.tail_budget
        if excess > 0:
            del self._tail[:excess]
            self.omitted += excess

    def render(self) -> bytes:
        if self.omitted == 0:
            return bytes(self._head + self._tail)
        marker = f"\n[... {self.omitted} bytes omitted ...]\n".encode()
        return bytes(self._head) + marker + bytes(self._tail)

    def reset(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self.omitted = 0


class _ReaderProtocol(asyncio.Protocol):
    """Drains the PTY master fd into the owning session's buffer."""

    def __init__(self, owner: "TerminalSession") -> None:
        self._owner = owner

    def data_received(self, data: bytes) -> None:
        self._owner._on_output(data)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._owner._on_closed()


class TerminalSession:
    """One persistent PTY-backed shell owned by a Role session.

    A background reader drains the PTY into a :class:`HeadTailBuffer` (for output)
    plus a short rolling tail (for marker detection). :meth:`collect` waits up to a
    deadline for the prompt marker / new output / the shell exiting, then renders
    the window and reports whether we are back at a prompt (with the exit code).
    """

    def __init__(self, *, session_key: str, cwd: Optional[str]) -> None:
        self.session_key = session_key
        self.cwd = cwd
        self.nonce = uuid.uuid4().hex[:12]
        self.mark = f"__TERM_{self.nonce}__"
        # Matches the PROMPT_COMMAND marker line: optional leading CR/LF, the mark,
        # a (possibly negative) exit code, __END, optional trailing CR/LF.
        self._marker_re = re.compile(
            rf"\r?\n?{re.escape(self.mark)}(-?\d+)__END\r?\n?".encode()
        )

        self._buffer = HeadTailBuffer()
        # Rolling raw tail for marker detection (the marker is short and arrives at
        # the end), so we never regex the whole capped buffer.
        self._recent = bytearray()
        self._output_event = asyncio.Event()
        self._closed = asyncio.Event()

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._master_fd: Optional[int] = None
        self._transport: Optional[asyncio.BaseTransport] = None
        self._wait_task: Optional[asyncio.Task] = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        import pty
        import termios

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        # Disable echo so the typed command line is not duplicated in the output.
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] = attrs[3] & ~termios.ECHO  # lflags
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass

        shell = os.environ.get("SHELL") or "/bin/bash"
        try:
            self._proc = await asyncio.create_subprocess_exec(
                shell,
                "--norc",
                "--noprofile",
                "--noediting",
                "-i",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.cwd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        loop = asyncio.get_event_loop()
        master_file = os.fdopen(master_fd, "rb", buffering=0)
        self._transport, _ = await loop.connect_read_pipe(
            lambda: _ReaderProtocol(self), master_file
        )
        self._wait_task = asyncio.ensure_future(self._wait_exit())

        # Install the sentinel prompt and consume the shell's startup banner +
        # the first marker, so the session is "ready" at a known prompt.
        setup = (
            f"PS1=''; PS2=''; "
            f"PROMPT_COMMAND='printf \"\\n{self.mark}%d__END\\n\" \"$?\"'\n"
        )
        os.write(master_fd, setup.encode())
        loop_time = loop.time()
        await self.collect(loop_time + _READY_TIMEOUT_S)

    async def _wait_exit(self) -> None:
        assert self._proc is not None
        try:
            await self._proc.wait()
        except Exception:  # noqa: BLE001
            pass
        self._on_closed()

    # --- output plumbing ---------------------------------------------------

    def _on_output(self, data: bytes) -> None:
        self._buffer.append(data)
        self._recent += data
        # Keep only enough tail to span a split marker.
        keep = len(self.mark) + 64
        if len(self._recent) > keep:
            del self._recent[: len(self._recent) - keep]
        self._output_event.set()

    def _on_closed(self) -> None:
        self._closed.set()
        self._output_event.set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def collect(self, deadline: float) -> tuple[str, Optional[int], bool, bool]:
        """Accumulate output until the prompt marker appears, the shell exits, or
        *deadline* passes.

        Returns ``(text, exit_code|None, at_prompt, closed)``:
          * ``at_prompt`` True  -> the marker was seen; ``exit_code`` is set; the
            shell is idle at a prompt (the command finished).
          * ``at_prompt`` False -> no marker within the window; a foreground program
            still owns the terminal (or output is still streaming).
          * ``closed`` True     -> the shell itself exited (PTY EOF).
        """
        loop = asyncio.get_event_loop()
        while True:
            if self._marker_re.search(self._recent) is not None:
                break
            if self._closed.is_set():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self._output_event.clear()
            if self._marker_re.search(self._recent) is not None or self._closed.is_set():
                break
            try:
                await asyncio.wait_for(self._output_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break

        rendered = self._buffer.render()
        self._buffer.reset()
        self._recent.clear()

        match = self._marker_re.search(rendered)
        if match is not None:
            text = _decode(rendered[: match.start()])
            try:
                exit_code = int(match.group(1).decode())
            except ValueError:
                exit_code = None
            return (text, exit_code, True, self._closed.is_set())

        text = _decode(rendered)
        return (text, None, False, self._closed.is_set())

    # --- input -------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if self._master_fd is None:
            raise ToolError("Error: terminal is not running.")
        try:
            os.write(self._master_fd, data)
        except OSError:
            raise ToolError("Error: terminal is closed.")

    async def feed(self, text: str, yield_ms: int) -> tuple[str, Optional[int], bool, bool]:
        """Type *text* into the terminal (a trailing newline is added if absent),
        then collect output for the yield window."""
        if text and not text.endswith("\n"):
            text = text + "\n"
        if text:
            self._write(text.encode())
        yield_ms = _clamp(yield_ms, MIN_YIELD_MS, MAX_YIELD_MS)
        loop = asyncio.get_event_loop()
        return await self.collect(loop.time() + yield_ms / 1000.0)

    async def interrupt(self, yield_ms: int) -> tuple[str, Optional[int], bool, bool]:
        """Send Ctrl-C to the foreground, then collect."""
        self._write(INTERRUPT.encode())
        yield_ms = _clamp(yield_ms, MIN_YIELD_MS, MAX_YIELD_MS)
        loop = asyncio.get_event_loop()
        return await self.collect(loop.time() + yield_ms / 1000.0)

    def shutdown(self) -> None:
        """Best-effort synchronous teardown (idempotent)."""
        if self._wait_task is not None and not self._wait_task.done():
            self._wait_task.cancel()
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        if self._master_fd is not None and self._transport is None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
        self._master_fd = None
