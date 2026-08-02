"""Persistent Python kernel driver — the backend for the single ``python`` tool.

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

The live :class:`KernelSession` is wrapped by :class:`KernelRuntimeDriver` and
registered with the Role's managed RuntimeHost (one implicit kernel per session,
with no model-facing id). The host owns identity, serialization, revision,
fencing and teardown; this module owns the kernel engine and its driver.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional, Protocol

from jupyter_client.manager import AsyncKernelManager

from mote.contracts.interaction.handoff import (
    DriverHandoffHandle,
    DriverHandoffResult,
    HandoffRequest,
    HumanHandoffOutcome,
)
from mote.contracts.runtime import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeHealth,
)
from mote.contracts.surface import (
    NOTEBOOK_MEDIA_TYPE,
    NotebookCell,
    NotebookDocument,
    NotebookExecuteInput,
    NotebookExportRepresentation,
    NotebookInputReply,
    NotebookInputRequest,
    NotebookOutput,
    SurfaceDescriptor,
    SurfaceFrame,
    SurfaceInput,
    SurfacePresentationMode,
)
from mote.contracts.tool.errors import ToolError
from mote.runtime.interactive.checkpoint_codec import (
    KERNEL_CHECKPOINT_CODEC,
    KernelCheckpointState,
    ShellCheckpointState,
)
from mote.runtime.interactive.kernel.notebook_export import export_notebook_ipynb
from mote.runtime.interactive.observation import SurfaceObservationHub
from mote.runtime.interactive.session_state import diff_env_state
from mote.runtime.telemetry.logging import logger
from mote.runtime.terminal_ansi import strip_ansi as _strip_ansi
from mote.runtime.text.elision import cap_head_tail

if TYPE_CHECKING:
    from jupyter_client.asynchronous.client import AsyncKernelClient


class _SandboxRuntime(Protocol):
    """The one method this module calls on the sandbox runtime (duck-typed).

    Structural only — keeps ``executor.dependency`` from importing the sandbox
    layer; any object exposing ``wrap_exec`` satisfies it.
    """

    async def wrap_exec(
        self,
        argv: list[str],
        *,
        cwd: Optional[str] = ...,
        env: Optional[dict[str, str]] = ...,
        extra_writable: Optional[list[str]] = ...,
    ) -> tuple[list[str], dict[str, str]]: ...


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
NOTEBOOK_MAX_CELLS = 256
NOTEBOOK_MAX_OUTPUTS = 256
NOTEBOOK_MAX_DISPLAY_CHARS = 5_592_408
NOTEBOOK_MAX_CELL_OUTPUT_CHARS = 8_388_608
NOTEBOOK_MAX_DOCUMENT_CHARS = 16_777_216

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_KERNEL_START_FAILED = "Error: Python kernel failed to start: {error}"
_MSG_KERNEL_RESTART_FAILED = "Error: Python kernel failed to restart: {error}"

# Timeout (s) for the internal, non-model-facing env probe / restore.
_PROBE_TIMEOUT_S = 5.0
# Env keys that are noise for a resume diff (per-process / launch bookkeeping):
# they vary on every kernel start and must not be re-applied into a fresh one.
# Anything jupyter/ipykernel injects at launch is already in the baseline (probed
# right after start), so only the per-process / launch-varying keys remain noise.
# PWD is doubly noise for a kernel: os.environ["PWD"] is NOT updated by os.chdir,
# so it is stale — the authoritative cwd comes from os.getcwd() (the probe's cwd).
_ENV_NOISE_KEYS = frozenset(
    {
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "JPY_PARENT_PID",
        "JPY_SESSION_NAME",
        "JPY_INTERRUPT_EVENT",
        "JPY_API_TOKEN",
        "KERNEL_LAUNCH_TIMEOUT",
    }
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


@dataclass(slots=True)
class KernelExecutionResult:
    """Text result for the tool plus structured output for notebook surfaces."""

    text: str
    timed_out: bool
    outputs: list[NotebookOutput]
    execution_count: int | None = None
    display_updates: list["KernelDisplayUpdate"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class KernelDisplayUpdate:
    """One validated ``update_display_data`` event from IOPub."""

    display_id: str
    data: dict[str, str]


class KernelSession:
    """One persistent ipykernel process owned by a Role session.

    Wraps a :class:`jupyter_client.AsyncKernelManager` + client. :meth:`execute`
    sends code on the shell channel and drains the iopub channel until the kernel
    returns to idle (or the deadline passes, after which the kernel is
    interrupted and the partial output returned).
    """

    def __init__(
        self,
        *,
        session_key: str,
        cwd: Optional[str],
        sandbox_runtime: Optional[_SandboxRuntime] = None,
    ) -> None:
        self.session_key = session_key
        self.cwd = cwd
        # Optional OS-level sandbox runtime. When set, start() wraps the kernel's
        # launch command (bwrap + hardened env) before spawning so the kernel —
        # and anything it forks — runs inside the sandbox. None => the historical
        # un-sandboxed kernel.
        #
        # NB (control-channel transport): inside the sandbox the kernel<->client
        # channels run over ipc:// unix sockets (filesystem-bound, NOT loopback
        # TCP), so they are unaffected by the network namespace. The socket
        # directory is bind-mounted into the sandbox via wrap_exec's
        # ``extra_writable`` (host client + sandboxed kernel see the same socket
        # inodes), so the kernel can safely run under the netns sole-egress chain.
        # The un-sandboxed kernel keeps the historical tcp transport + manager
        # ``{connection_file}`` substitution.
        self.sandbox_runtime = sandbox_runtime
        # Host temp dir holding the ipc:// sockets + connection file when
        # sandboxed; bind-mounted into the sandbox and rmtree'd at teardown.
        self._sock_dir: Optional[str] = None
        self._km: "AsyncKernelManager | None" = None
        self._kc: "AsyncKernelClient | None" = None
        self._closed = False
        # Snapshot of the kernel's launch env, baseline for capture_state()'s
        # diff. Empty until start() probes it.
        self._baseline_env: dict[str, str] = {}

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        km = AsyncKernelManager()
        self._km = km
        start_kwargs: dict = {"cwd": self.cwd}
        # When a sandbox runtime is wired, wrap the kernel launch command so the
        # kernel runs inside the sandbox.
        #
        # The control channels use ipc:// unix sockets (not loopback TCP) so they
        # survive the network namespace the sandbox may unshare for sole-egress.
        # unix sockets live on the filesystem, so we put them in a short-pathed
        # host temp dir (AF_UNIX caps paths at ~108 chars; the default jupyter
        # runtime dir + uuid can overflow) and bind that dir read-write into the
        # sandbox via ``extra_writable`` so the host client and sandboxed kernel
        # share the same socket inodes.
        #
        # The connection file is PRE-RESOLVED here (write_connection_file) rather
        # than relying on the manager's ``{connection_file}`` substitution: under
        # the netns launcher the payload argv is base64-encoded into a config
        # token, so the manager can't find/replace a brace nested inside it. A
        # concrete ``-f <path>`` sidesteps that for both the direct-bwrap and
        # netns paths.
        if self.sandbox_runtime is not None:
            self._sock_dir = tempfile.mkdtemp(prefix="mgk-")
            km.transport = "ipc"
            # Pin the connection file inside the (bind-mounted) socket dir so the
            # sandboxed kernel can read it. We set ``connection_file`` directly
            # rather than ``connection_dir`` because write_connection_file ignores
            # the latter and would otherwise drop a random ``/tmp/tmpXXX.json``
            # that bwrap's ``--tmpfs /tmp`` masks out (unreadable inside).
            km.connection_file = os.path.join(self._sock_dir, "conn.json")
            # Absolute socket prefix => k-1 .. k-5. A relative prefix would be
            # resolved against each side's cwd and never line up.
            km.ip = os.path.join(self._sock_dir, "k")
            km.write_connection_file()
            conn = km.connection_file
            base_cmd = [sys.executable, "-m", "ipykernel_launcher", "-f", conn]
            wrapped, env = await self.sandbox_runtime.wrap_exec(
                base_cmd,
                cwd=self.cwd,
                env=dict(os.environ),
                extra_writable=[self._sock_dir],
            )
            # Inject the wrapped launch command via the kernel spec's argv — the
            # seam the local provisioner actually reads (format_kernel_cmd builds
            # from ``kernel_spec.argv``). The ``KernelManager.kernel_cmd`` trait
            # is NOT honoured by jupyter_client's provisioner path, so setting it
            # would silently leave the kernel un-sandboxed. The argv carries a
            # concrete ``-f <conn>`` (no ``{connection_file}`` brace), so
            # format_kernel_cmd's substitution is a harmless no-op.
            kernel_spec = km.kernel_spec
            assert kernel_spec is not None
            kernel_spec.argv = wrapped
            start_kwargs["env"] = env
        await km.start_kernel(**start_kwargs)
        kc = km.client()
        self._kc = kc
        kc.start_channels()
        try:
            await kc.wait_for_ready(timeout=_READY_TIMEOUT_S)
        except RuntimeError as e:
            self.kill()
            raise ToolError(_MSG_KERNEL_START_FAILED.format(error=e))
        # Snapshot the kernel's launch env as the baseline for capture_state()'s
        # diff (best-effort; an empty baseline just makes the first diff report
        # the full env, which is harmless).
        probed = await self._probe_env()
        if probed is not None:
            self._baseline_env = probed[1]

    @property
    def closed(self) -> bool:
        return self._closed or self._km is None

    @property
    def _client(self) -> "AsyncKernelClient":
        """The kernel client; populated by start(). Asserts it is live so the
        operation methods (which only run post-start) narrow away the Optional."""
        assert self._kc is not None, "kernel client used before start()"
        return self._kc

    @property
    def _manager(self) -> "AsyncKernelManager":
        """The kernel manager; populated by start(). Same post-start contract."""
        assert self._km is not None, "kernel manager used before start()"
        return self._km

    # --- output plumbing ---------------------------------------------------

    @staticmethod
    def _accumulate(
        msg: dict,
        parts: list[str],
        outputs: list[NotebookOutput] | None = None,
        execution_count: list[int | None] | None = None,
        display_updates: list[KernelDisplayUpdate] | None = None,
    ) -> bool:
        """Append a single iopub message's payload to *parts*.

        Returns True when this is the terminal ``status: idle`` message.
        """
        msg_type = msg["msg_type"]
        content = msg["content"]
        if msg_type == "stream":
            text = str(content.get("text", ""))
            parts.append(text)
            KernelSession._append_output(
                outputs,
                NotebookOutput(
                    output_type="stream",
                    name=content.get("name") if content.get("name") in {"stdout", "stderr"} else "stdout",
                    text=cap_head_tail(text, OUTPUT_MAX_CHARS)[0],
                ),
            )
        elif msg_type == "execute_input":
            count = content.get("execution_count")
            if execution_count is not None and isinstance(count, int) and count >= 0:
                execution_count[0] = count
        elif msg_type in ("execute_result", "display_data", "update_display_data"):
            data = content.get("data", {})
            text = data.get("text/plain")
            if text:
                parts.append(text if text.endswith("\n") else text + "\n")
            safe_data: dict[str, str] = {}
            for media_type in ("text/plain", "image/png"):
                value = data.get(media_type)
                if not isinstance(value, str):
                    continue
                if len(value) <= NOTEBOOK_MAX_DISPLAY_CHARS:
                    safe_data[media_type] = value
            count = content.get("execution_count")
            count = count if isinstance(count, int) and count >= 0 else None
            if execution_count is not None and count is not None:
                execution_count[0] = count
            display_id = KernelSession._display_id(content)
            if msg_type == "update_display_data":
                if display_id is not None and display_updates is not None:
                    display_updates.append(KernelDisplayUpdate(display_id=display_id, data=safe_data))
            else:
                KernelSession._append_output(
                    outputs,
                    NotebookOutput(
                        output_type=msg_type,
                        data=safe_data,
                        execution_count=count,
                        display_id=display_id,
                    ),
                )
        elif msg_type == "error":
            traceback = [_strip_ansi(str(line))[:65_536] for line in content.get("traceback", [])[:256]]
            parts.append("\n".join(traceback) + "\n")
            KernelSession._append_output(
                outputs,
                NotebookOutput(
                    output_type="error",
                    ename=str(content.get("ename", ""))[:512],
                    evalue=str(content.get("evalue", ""))[:4096],
                    traceback=traceback,
                ),
            )
        elif msg_type == "status" and content.get("execution_state") == "idle":
            return True
        return False

    @staticmethod
    def _display_id(content: dict) -> str | None:
        transient = content.get("transient", {})
        if not isinstance(transient, dict):
            return None
        display_id = transient.get("display_id")
        if not isinstance(display_id, str) or not 0 < len(display_id) <= 256:
            return None
        return display_id

    @staticmethod
    def _append_output(outputs: list[NotebookOutput] | None, output: NotebookOutput) -> None:
        if outputs is None or len(outputs) >= NOTEBOOK_MAX_OUTPUTS:
            return
        used = sum(KernelSession._output_size(item) for item in outputs)
        if used + KernelSession._output_size(output) <= NOTEBOOK_MAX_CELL_OUTPUT_CHARS:
            outputs.append(output)

    @staticmethod
    def _output_size(output: NotebookOutput) -> int:
        return (
            len(output.text)
            + sum(len(value) for value in output.data.values())
            + sum(len(line) for line in output.traceback)
        )

    async def _drain(
        self,
        msg_id: str,
        parts: list[str],
        deadline: float,
        outputs: list[NotebookOutput] | None = None,
        execution_count: list[int | None] | None = None,
        display_updates: list[KernelDisplayUpdate] | None = None,
        input_callback: Callable[[NotebookInputRequest], Awaitable[None]] | None = None,
        cell_id: str = "",
    ) -> bool:
        """Drain iopub messages for *msg_id* until idle or *deadline*.

        Returns True if idle was reached, False on timeout.
        """
        loop = asyncio.get_event_loop()
        channels = {"iopub": self._client.get_iopub_msg}
        if input_callback is not None:
            channels["stdin"] = self._client.get_stdin_msg
        pending: dict[asyncio.Task, str] = {}

        def schedule(channel: str) -> None:
            remaining = deadline - loop.time()
            if remaining > 0:
                pending[asyncio.create_task(channels[channel](timeout=remaining))] = channel

        for channel in channels:
            schedule(channel)
        try:
            while pending:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                done, _ = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    return False
                for task in done:
                    channel = pending.pop(task)
                    try:
                        msg = task.result()
                    except queue.Empty:
                        return False
                    if msg["parent_header"].get("msg_id") != msg_id:
                        schedule(channel)
                        continue
                    if channel == "stdin":
                        content = msg.get("content", {})
                        request_id = str(msg.get("header", {}).get("msg_id", ""))
                        if not request_id:
                            request_id = uuid.uuid4().hex
                        assert input_callback is not None
                        await input_callback(
                            NotebookInputRequest(
                                request_id=request_id,
                                cell_id=cell_id,
                                prompt=str(content.get("prompt", ""))[:65_536],
                                password=bool(content.get("password", False)),
                            )
                        )
                        schedule(channel)
                        continue
                    if self._accumulate(
                        msg,
                        parts,
                        outputs,
                        execution_count,
                        display_updates,
                    ):
                        return True
                    schedule(channel)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return False

    # --- operations --------------------------------------------------------

    async def execute(self, code: str, timeout: float) -> tuple[str, bool]:
        """Run *code*, block until idle or *timeout*; return (text, timed_out).

        On timeout the kernel is interrupted (its state is preserved) and a short
        grace window drains the KeyboardInterrupt traceback + idle marker.
        """
        result = await self.execute_detailed(code, timeout)
        return result.text, result.timed_out

    async def execute_detailed(
        self,
        code: str,
        timeout: float,
        *,
        cell_id: str = "",
        input_callback: Callable[[NotebookInputRequest], Awaitable[None]] | None = None,
    ) -> KernelExecutionResult:
        """Execute code and retain frontend-safe structured IOPub outputs."""
        timeout = _clamp(timeout, MIN_TIMEOUT_S, MAX_TIMEOUT_S)
        msg_id = self._client.execute(code, allow_stdin=input_callback is not None)
        loop = asyncio.get_event_loop()
        parts: list[str] = []
        outputs: list[NotebookOutput] = []
        execution_count: list[int | None] = [None]
        display_updates: list[KernelDisplayUpdate] = []
        idle = await self._drain(
            msg_id,
            parts,
            loop.time() + timeout,
            outputs,
            execution_count,
            display_updates,
            input_callback,
            cell_id,
        )
        if idle:
            return KernelExecutionResult(
                text=cap_head_tail("".join(parts), OUTPUT_MAX_CHARS)[0],
                timed_out=False,
                outputs=outputs,
                execution_count=execution_count[0],
                display_updates=display_updates,
            )
        # Timed out: interrupt and drain the aftermath for a short grace window.
        await self._manager.interrupt_kernel()
        await self._drain(
            msg_id,
            parts,
            loop.time() + _INTERRUPT_GRACE_S,
            outputs,
            execution_count,
            display_updates,
            input_callback,
            cell_id,
        )
        return KernelExecutionResult(
            text=cap_head_tail("".join(parts), OUTPUT_MAX_CHARS)[0],
            timed_out=True,
            outputs=outputs,
            execution_count=execution_count[0],
            display_updates=display_updates,
        )

    def reply_input(self, value: str) -> None:
        """Reply on the kernel stdin channel to its current request."""
        self._client.input(value)

    async def cancel_input(self) -> None:
        """Interrupt an execution that is blocked awaiting stdin."""
        await self._manager.interrupt_kernel()

    async def interrupt(self) -> str:
        """Send a KeyboardInterrupt to the kernel and drain any aftermath."""
        await self._manager.interrupt_kernel()
        loop = asyncio.get_event_loop()
        parts: list[str] = []
        await self._drain("", parts, loop.time() + _INTERRUPT_GRACE_S)
        return cap_head_tail("".join(parts), OUTPUT_MAX_CHARS)[0]

    # --- state capture / restore (for session resume) ----------------------

    async def _run_internal(self, code: str, timeout: float) -> Optional[str]:
        """Run non-model-facing code WITHOUT touching the execution history.

        ``store_history=False`` keeps stdout streaming (so the probe's ``print``
        is captured) while leaving the kernel's ``execution_count`` and IPython's
        ``_`` / ``Out`` / ``In`` untouched — the kernel analog of the terminal's
        echo-disabled, non-model-facing probe. Returns the drained stdout text,
        or ``None`` on timeout / failure (best-effort).
        """
        try:
            msg_id = self._client.execute(code, store_history=False, allow_stdin=False)
            loop = asyncio.get_event_loop()
            parts: list[str] = []
            if not await self._drain(msg_id, parts, loop.time() + timeout):
                return None
            return "".join(parts)
        except Exception as exc:  # noqa: BLE001 — internal probe/restore is best-effort
            logger.debug(f"Kernel: internal probe/restore failed: {exc}")
            return None

    async def _probe_env(self) -> Optional[tuple[str, dict[str, str]]]:
        """Run a non-model-facing probe and parse out ``(cwd, env)``.

        Runs a one-liner that prints ``{"cwd", "env"}`` as JSON wrapped in a
        private nonce sentinel. Uses inline ``__import__`` so it binds **no**
        names in the user namespace, and ``store_history=False`` so it does not
        advance the execution counter. JSON serialization sidesteps the shell
        probe's multi-line / quoting pitfalls entirely.

        Best-effort: any failure returns ``None``.
        """
        try:
            nonce = uuid.uuid4().hex[:12]
            begin = f"__KPROBE_{nonce}__"
            end = f"__KEND_{nonce}__"
            code = (
                f"print('{begin}' + __import__('json').dumps("
                f"{{'cwd': __import__('os').getcwd(), "
                f"'env': dict(__import__('os').environ)}}) + '{end}')"
            )
            text = await self._run_internal(code, _PROBE_TIMEOUT_S)
            if not text:
                return None
            start = text.find(begin)
            stop = text.find(end)
            if start == -1 or stop == -1 or stop < start:
                return None
            payload = json.loads(text[start + len(begin) : stop])
            env = {str(k): str(v) for k, v in dict(payload.get("env", {})).items()}
            return (str(payload.get("cwd", "")), env)
        except Exception as exc:  # noqa: BLE001 — capture is best-effort
            logger.debug(f"Kernel: env capture/parse failed: {exc}")
            return None

    async def capture_state(self) -> Optional[tuple[str, dict[str, str], list[str]]]:
        """Capture ``(cwd, env_diff, unset)`` relative to the launch baseline.

        ``env_diff`` = keys added/changed since launch; ``unset`` = keys present
        at launch but now gone. Noise keys (per-process / launch bookkeeping) are
        filtered out of both. Best-effort: returns ``None`` on any failure (e.g.
        the kernel is not idle/healthy).
        """
        return diff_env_state(await self._probe_env(), self._baseline_env, _ENV_NOISE_KEYS)

    async def restore_state(self, cwd: str, env: dict[str, str], unset: list[str]) -> None:
        """Re-seed a fresh kernel to a saved ``(cwd, env, unset)`` state.

        Injects ``os.chdir`` + ``os.environ.update`` + ``pop`` as one
        non-model-facing cell (``store_history=False``, binds no names). Values
        are embedded with ``repr()`` — the Python analog of the terminal's
        single-quote escaping — so a str/dict/list-of-str yields a safe literal
        and no embedded code is evaluated. Restores cwd + env only, never the
        Python namespace. Best-effort: never raises.
        """
        try:
            env = {k: v for k, v in env.items() if k not in _ENV_NOISE_KEYS and k.isidentifier()}
            unset = [k for k in unset if k.isidentifier() and k not in _ENV_NOISE_KEYS]
            lines: list[str] = []
            if cwd:
                lines.append(f"__import__('os').chdir({cwd!r})")
            if env:
                lines.append(f"__import__('os').environ.update({env!r})")
            if unset:
                lines.append(f"[__import__('os').environ.pop(_k, None) for _k in {unset!r}]")
            if not lines:
                return
            await self._run_internal("\n".join(lines), _PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — restore is best-effort
            logger.debug(f"Kernel: env restore failed: {exc}")

    async def restart(self) -> None:
        """Restart the kernel — clears all in-memory state."""
        await self._manager.restart_kernel(now=True)
        try:
            await self._client.wait_for_ready(timeout=_READY_TIMEOUT_S)
        except RuntimeError as e:
            self.kill()
            raise ToolError(_MSG_KERNEL_RESTART_FAILED.format(error=e))

    async def shutdown(self) -> None:
        """Graceful async teardown."""
        self._closed = True
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Kernel: stop_channels during shutdown failed: {exc}")
        if self._km is not None:
            try:
                await self._km.shutdown_kernel(now=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Kernel: shutdown_kernel failed, killing: {exc}")
                self.kill()
        self._km = None
        self._kc = None
        self._cleanup_sock_dir()

    def kill(self) -> None:
        """Best-effort synchronous teardown (idempotent) — for cleanup_session.

        ``shutdown_kernel`` is a coroutine; cleanup runs synchronously, so we
        SIGKILL the kernel's launched process directly.
        """
        self._closed = True
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Kernel: stop_channels during kill failed: {exc}")
            self._kc = None
        if self._km is not None:
            proc = getattr(getattr(self._km, "provisioner", None), "process", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            self._km = None
        self._cleanup_sock_dir()

    def _cleanup_sock_dir(self) -> None:
        """Remove the ephemeral ipc:// socket dir, if any (idempotent)."""
        if self._sock_dir is not None:
            shutil.rmtree(self._sock_dir, ignore_errors=True)
            self._sock_dir = None


class KernelRuntimeDriver:
    """Managed-runtime adapter for one persistent Jupyter kernel."""

    kind = "jupyter"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.LOGICAL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"notebook"}),
        multi_instance=False,
    )

    def __init__(
        self,
        *,
        session_key: str,
        cwd: Optional[str],
        sandbox_runtime: Optional[_SandboxRuntime] = None,
    ) -> None:
        self._session_key = session_key
        self._cwd = cwd
        self._sandbox_runtime = sandbox_runtime
        self._session: KernelSession | None = None
        self._handoff_id: str | None = None
        self._surface_ref = f"jupyter-session:{session_key}"
        self._cells: list[NotebookCell] = []
        self._cells_truncated = False
        self._kernel_epoch = 0
        self._kernel_status: Literal["idle", "busy", "restarting", "stopped"] = "idle"
        self._surface_sequence = 0
        self._surface_observers = SurfaceObservationHub()
        self._input_request: NotebookInputRequest | None = None
        self._active_human_cell_id: str | None = None

    @property
    def session(self) -> KernelSession:
        session = self._session
        if session is None:
            raise ToolError("Error: Python kernel is not running.")
        return session

    @property
    def closed(self) -> bool:
        return self._session is None or self._session.closed

    async def start(self, checkpoint: RuntimeCheckpoint | None = None) -> DriverStartResult:
        if self._session is not None:
            raise RuntimeError("kernel runtime is already started")
        session = KernelSession(
            session_key=self._session_key,
            cwd=self._cwd,
            sandbox_runtime=self._sandbox_runtime,
        )
        self._session = session
        try:
            await session.start()
            restore = self._decode_checkpoint(checkpoint) if checkpoint is not None else None
            if restore:
                await session.restore_state(
                    restore.shell.cwd,
                    dict(restore.shell.env),
                    list(restore.shell.unset),
                )
                if restore.notebook is not None:
                    notebook = restore.notebook
                    self._surface_ref = notebook.ref
                    self._cells = [cell.model_copy(deep=True) for cell in notebook.cells]
                    self._cells_truncated = notebook.truncated
                    self._kernel_epoch = notebook.kernel_epoch + 1
                    self._kernel_status = "idle"
                    self._surface_sequence = notebook.revision
        except BaseException:
            session.kill()
            self._session = None
            raise
        return DriverStartResult(restored=bool(restore))

    async def health(self) -> RuntimeHealth:
        if self._session is None:
            return RuntimeHealth(healthy=False, status="stopped", detail="kernel has not started")
        if self._session.closed:
            return RuntimeHealth(healthy=False, status="exited", detail="kernel process exited")
        return RuntimeHealth(healthy=True)

    async def checkpoint(self, reason: str) -> DriverCheckpoint:
        state = await self.session.capture_state()
        if state is None:
            raise RuntimeError("kernel logical state is unavailable")
        cwd, env, unset = state
        return KERNEL_CHECKPOINT_CODEC.encode(
            KernelCheckpointState(
                ShellCheckpointState(cwd, env, tuple(unset)),
                self.snapshot_document(),
            ),
            fidelity=CheckpointFidelity.LOGICAL,
        )

    async def execute(
        self,
        code: str,
        timeout: float,
        *,
        origin: Literal["agent", "human"] = "agent",
        cell_id: str | None = None,
    ) -> tuple[str, bool]:
        cell = NotebookCell(
            id=cell_id or f"cell-{uuid.uuid4().hex}",
            source=code,
            status="running",
            origin=origin,
        )
        if any(existing.id == cell.id for existing in self._cells):
            raise ValueError(f"notebook cell id already exists: {cell.id}")
        self._append_cell(cell)
        if origin == "human":
            if self._active_human_cell_id is not None:
                raise RuntimeError("jupyter already has a running human cell")
            self._active_human_cell_id = cell.id
        self._kernel_status = "busy"
        self._publish_surface()
        try:
            input_callback = self._set_input_request if origin == "human" else None
            result = await self.session.execute_detailed(
                code,
                timeout,
                cell_id=cell.id,
                input_callback=input_callback,
            )
        except BaseException:
            cell.status = "error"
            self._kernel_status = "idle"
            self._clear_input_request(cell.id)
            if self._active_human_cell_id == cell.id:
                self._active_human_cell_id = None
            self._publish_surface()
            raise
        cell.outputs = result.outputs
        for update in result.display_updates:
            self._replace_display(update)
        cell.execution_count = result.execution_count
        cell.status = "timed_out" if result.timed_out else self._cell_status(result.outputs)
        self._trim_cells()
        self._kernel_status = "idle"
        self._clear_input_request(cell.id)
        if self._active_human_cell_id == cell.id:
            self._active_human_cell_id = None
        self._publish_surface()
        return result.text, result.timed_out

    async def interrupt(self) -> str:
        output = await self.session.interrupt()
        self._kernel_status = "idle"
        self._publish_surface()
        return output

    async def restart(self) -> None:
        self._kernel_status = "restarting"
        self._publish_surface()
        try:
            await self.session.restart()
        except BaseException:
            self._kernel_status = "stopped" if self.session.closed else "idle"
            self._publish_surface()
            raise
        self._kernel_epoch += 1
        self._kernel_status = "idle"
        self._publish_surface()

    async def capture_state(self) -> Optional[tuple[str, dict[str, str], list[str]]]:
        return await self.session.capture_state()

    async def prepare_handoff(self, request: HandoffRequest) -> DriverHandoffHandle:
        if self._handoff_id is not None:
            raise RuntimeError("jupyter runtime is already handed off")
        if self.closed:
            raise ToolError("Error: Python kernel is not running.")
        self._handoff_id = uuid.uuid4().hex
        self._surface_observers.attach(self._handoff_id)
        self._surface_ref = f"jupyter-notebook:{request.runtime_ref.runtime_id}"
        return DriverHandoffHandle(
            handle_id=self._handoff_id,
            surface=SurfaceDescriptor(
                kind="notebook",
                ref=self._surface_ref,
                presentation=SurfacePresentationMode.WINDOW,
                title="Jupyter Notebook",
            ),
        )

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult:
        if handle.handle_id != self._handoff_id:
            raise RuntimeError("jupyter handoff handle is not current")
        if self._active_human_cell_id is not None:
            await self.session.cancel_input()
        if self._input_request is not None:
            self._input_request = None
            self._publish_surface()
        self._handoff_id = None
        return DriverHandoffResult(summary="Human returned control of the Jupyter runtime.")

    async def snapshot_surface(self, handle: DriverHandoffHandle) -> SurfaceFrame:
        self._assert_surface_handle(handle)
        document = self.snapshot_document()
        return SurfaceFrame(
            sequence=self._surface_sequence,
            media_type=NOTEBOOK_MEDIA_TYPE,
            content=document.model_dump_json(),
        )

    def snapshot_document(self) -> NotebookDocument:
        """Return an isolated immutable-by-convention Notebook snapshot."""
        return NotebookDocument(
            ref=self._surface_ref,
            revision=self._surface_sequence,
            kernel_epoch=self._kernel_epoch,
            kernel_status=self._kernel_status,
            cells=[cell.model_copy(deep=True) for cell in self._cells],
            input_request=(self._input_request.model_copy(deep=True) if self._input_request is not None else None),
            truncated=self._cells_truncated,
        )

    async def export_representations(
        self,
        document: NotebookDocument,
    ) -> tuple[NotebookExportRepresentation, ...]:
        """Export a snapshot without consulting the live kernel or mutable cells."""
        return (export_notebook_ipynb(document),)

    async def next_surface_frame(
        self,
        handle: DriverHandoffHandle,
        after_sequence: int,
    ) -> SurfaceFrame | None:
        self._assert_surface_handle(handle)
        changed = await self._surface_observers.wait_for_change(
            handle.handle_id,
            after_sequence,
            lambda: self._surface_sequence,
        )
        return await self.snapshot_surface(handle) if changed else None

    async def detach_surface(self, handle: DriverHandoffHandle) -> None:
        self._surface_observers.detach(handle.handle_id)

    async def send_surface_input(self, handle: DriverHandoffHandle, event: SurfaceInput) -> None:
        self._assert_handoff_handle(handle)
        if event.kind == "notebook.execute":
            submitted = NotebookExecuteInput.model_validate_json(event.data)
            await self.execute(
                submitted.source,
                DEFAULT_TIMEOUT_S,
                origin="human",
                cell_id=submitted.cell_id,
            )
            return
        if event.kind == "notebook.input_reply":
            reply = NotebookInputReply.model_validate_json(event.data)
            pending = self._input_request
            if pending is None or pending.request_id != reply.request_id:
                raise RuntimeError("jupyter input request is not current")
            self.session.reply_input(reply.value)
            self._input_request = None
            self._publish_surface()
            return
        raise ValueError(f"unsupported jupyter surface input: {event.kind}")

    async def aclose(self) -> None:
        self._handoff_id = None
        self._kernel_status = "stopped"
        self._surface_observers.close()
        session, self._session = self._session, None
        if session is not None:
            await session.shutdown()

    def _append_cell(self, cell: NotebookCell) -> None:
        self._cells.append(cell)
        if len(self._cells) > NOTEBOOK_MAX_CELLS:
            del self._cells[: len(self._cells) - NOTEBOOK_MAX_CELLS]
            self._cells_truncated = True

    async def _set_input_request(self, request: NotebookInputRequest) -> None:
        if self._handoff_id is None:
            raise RuntimeError("jupyter stdin requires an active human handoff")
        if self._input_request is not None:
            raise RuntimeError("jupyter already has a pending input request")
        self._input_request = request
        self._publish_surface()

    def _clear_input_request(self, cell_id: str) -> None:
        if self._input_request is not None and self._input_request.cell_id == cell_id:
            self._input_request = None

    def _replace_display(self, update: KernelDisplayUpdate) -> None:
        for cell in self._cells:
            for index, output in enumerate(cell.outputs):
                if output.display_id == update.display_id:
                    cell.outputs[index] = output.model_copy(update={"data": dict(update.data)})

    def _trim_cells(self) -> None:
        size = sum(self._cell_size(cell) for cell in self._cells)
        while len(self._cells) > 1 and size > NOTEBOOK_MAX_DOCUMENT_CHARS:
            size -= self._cell_size(self._cells.pop(0))
            self._cells_truncated = True

    @staticmethod
    def _cell_size(cell: NotebookCell) -> int:
        return len(cell.source) + sum(KernelSession._output_size(output) for output in cell.outputs)

    def _publish_surface(self) -> None:
        self._surface_sequence += 1
        self._surface_observers.notify()

    @staticmethod
    def _cell_status(
        outputs: list[NotebookOutput],
    ) -> Literal["complete", "error"]:
        return "error" if any(output.output_type == "error" for output in outputs) else "complete"

    def _assert_handoff_handle(self, handle: DriverHandoffHandle) -> None:
        if handle.handle_id != self._handoff_id:
            raise RuntimeError("jupyter handoff handle is not current")

    def _assert_surface_handle(self, handle: DriverHandoffHandle) -> None:
        if not self._surface_observers.contains(handle.handle_id):
            raise RuntimeError("jupyter surface attachment is not current")

    @staticmethod
    def _decode_checkpoint(checkpoint: RuntimeCheckpoint) -> KernelCheckpointState:
        return KERNEL_CHECKPOINT_CODEC.decode(checkpoint)
