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
import json
import queue
import re
import signal
import uuid
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

    def __init__(self, *, session_key: str, cwd: Optional[str], sandbox_runtime: Optional[object] = None) -> None:
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
        self._km = None  # AsyncKernelManager
        self._kc = None  # AsyncKernelClient
        self._closed = False
        # Snapshot of the kernel's launch env, baseline for capture_state()'s
        # diff. Empty until start() probes it.
        self._baseline_env: dict[str, str] = {}

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        import os

        from jupyter_client.manager import AsyncKernelManager

        self._km = AsyncKernelManager()
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
            import sys
            import tempfile

            self._sock_dir = tempfile.mkdtemp(prefix="mgk-")
            self._km.transport = "ipc"
            # Pin the connection file inside the (bind-mounted) socket dir so the
            # sandboxed kernel can read it. We set ``connection_file`` directly
            # rather than ``connection_dir`` because write_connection_file ignores
            # the latter and would otherwise drop a random ``/tmp/tmpXXX.json``
            # that bwrap's ``--tmpfs /tmp`` masks out (unreadable inside).
            self._km.connection_file = os.path.join(self._sock_dir, "conn.json")
            # Absolute socket prefix => k-1 .. k-5. A relative prefix would be
            # resolved against each side's cwd and never line up.
            self._km.ip = os.path.join(self._sock_dir, "k")
            self._km.write_connection_file()
            conn = self._km.connection_file
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
            self._km.kernel_spec.argv = wrapped
            start_kwargs["env"] = env
        await self._km.start_kernel(**start_kwargs)
        self._kc = self._km.client()
        self._kc.start_channels()
        try:
            await self._kc.wait_for_ready(timeout=_READY_TIMEOUT_S)
        except RuntimeError as e:
            self.kill()
            raise ToolError(f"Error: Python kernel failed to start: {e}")
        # Snapshot the kernel's launch env as the baseline for capture_state()'s
        # diff (best-effort; an empty baseline just makes the first diff report
        # the full env, which is harmless).
        probed = await self._probe_env()
        if probed is not None:
            self._baseline_env = probed[1]

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
            msg_id = self._kc.execute(code, store_history=False, allow_stdin=False)
            loop = asyncio.get_event_loop()
            parts: list[str] = []
            if not await self._drain(msg_id, parts, loop.time() + timeout):
                return None
            return "".join(parts)
        except Exception:  # noqa: BLE001 — internal probe/restore is best-effort
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
            payload = json.loads(text[start + len(begin):stop])
            env = {
                str(k): str(v)
                for k, v in dict(payload.get("env", {})).items()
            }
            return (str(payload.get("cwd", "")), env)
        except Exception:  # noqa: BLE001 — capture is best-effort
            return None

    async def capture_state(self) -> Optional[tuple[str, dict[str, str], list[str]]]:
        """Capture ``(cwd, env_diff, unset)`` relative to the launch baseline.

        ``env_diff`` = keys added/changed since launch; ``unset`` = keys present
        at launch but now gone. Noise keys (per-process / launch bookkeeping) are
        filtered out of both. Best-effort: returns ``None`` on any failure (e.g.
        the kernel is not idle/healthy).
        """
        probed = await self._probe_env()
        if probed is None:
            return None
        cwd, env = probed
        diff: dict[str, str] = {}
        for key, value in env.items():
            if key in _ENV_NOISE_KEYS:
                continue
            if self._baseline_env.get(key) != value:
                diff[key] = value
        unset = [
            key
            for key in self._baseline_env
            if key not in env and key not in _ENV_NOISE_KEYS
        ]
        return (cwd, diff, unset)

    async def restore_state(
        self, cwd: str, env: dict[str, str], unset: list[str]
    ) -> None:
        """Re-seed a fresh kernel to a saved ``(cwd, env, unset)`` state.

        Injects ``os.chdir`` + ``os.environ.update`` + ``pop`` as one
        non-model-facing cell (``store_history=False``, binds no names). Values
        are embedded with ``repr()`` — the Python analog of the terminal's
        single-quote escaping — so a str/dict/list-of-str yields a safe literal
        and no embedded code is evaluated. Restores cwd + env only, never the
        Python namespace. Best-effort: never raises.
        """
        try:
            env = {
                k: v
                for k, v in env.items()
                if k not in _ENV_NOISE_KEYS and k.isidentifier()
            }
            unset = [k for k in unset if k.isidentifier() and k not in _ENV_NOISE_KEYS]
            lines: list[str] = []
            if cwd:
                lines.append(f"__import__('os').chdir({cwd!r})")
            if env:
                lines.append(f"__import__('os').environ.update({env!r})")
            if unset:
                lines.append(
                    f"[__import__('os').environ.pop(_k, None) for _k in {unset!r}]"
                )
            if not lines:
                return
            await self._run_internal("\n".join(lines), _PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — restore is best-effort
            pass

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
        self._cleanup_sock_dir()

    def _cleanup_sock_dir(self) -> None:
        """Remove the ephemeral ipc:// socket dir, if any (idempotent)."""
        if self._sock_dir is not None:
            import shutil

            shutil.rmtree(self._sock_dir, ignore_errors=True)
            self._sock_dir = None
