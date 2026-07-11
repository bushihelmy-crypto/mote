"""python — one persistent Python (Jupyter) kernel the model executes code into.

The Python sibling of the persistent :class:`Terminal` tool: there is **one
implicit kernel per Role session** (no kernel id to track, like a notebook), and
the model drives it by executing code:

- ``code`` is run in the kernel. Top-level variables, imports, and defined
  functions persist across calls, so you can build up state step by step.
- ``interrupt=True`` sends a KeyboardInterrupt to a wedged kernel.
- ``restart=True`` rebuilds a clean kernel (clears all state).
- ``close=True`` shuts the kernel down.

Each ``code`` call blocks until the kernel returns to idle (the cell finished)
or ``timeout`` seconds elapse, in which case the kernel is interrupted and the
partial output is returned (its state is preserved). Use this for interactive,
stateful Python work; use the one-shot :class:`Bash` tool for shell commands and
the :class:`Terminal` tool when you need a persistent shell or to drive an
arbitrary interactive program.

The live :class:`KernelSession` is owned by the Role: it is stored on the Role's
``RoleState`` (via the ``get_tool_session`` / ``set_tool_session`` capabilities)
rather than a process-global singleton, so each Role's kernel is isolated and
torn down with it.
"""
from __future__ import annotations

import os
from typing import Any, Callable, ClassVar, Optional

from mote.common.logs import logger
from mote.common.prompt.tools import PYTHON_DESCRIPTION
from mote.executor.base_tool import BaseTool
from mote.executor.dependency._kernel import DEFAULT_TIMEOUT_S, KernelSession
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_NO_KERNEL_TO_INTERRUPT = "Error: no live kernel to interrupt."
_MSG_KERNEL_FAILED = "Error running Python kernel: {error}"


@register_tool
class Python(BaseTool):
    """Execute code in a persistent Python kernel (one per session)."""

    name = "Jupyter"
    aliases = ["Python"]
    max_result_size_chars: ClassVar[int] = 30_000
    description = PYTHON_DESCRIPTION
    requires = (
        "get_cwd",
        "get_tool_session",
        "set_tool_session",
        "get_sandbox_runtime",
        "record_kernel_state",
        "take_pending_kernel_restore",
    )
    # Arbitrary code execution.
    risk_level = "high"
    # Holds a live Jupyter kernel on RoleState between calls.
    stateful = True

    # Injected from Role by bind(): the cwd accessor (seeds the kernel's initial
    # working directory on first use) + the per-Role tool-session store (where
    # the live KernelSession is kept, so it persists across calls and is owned
    # by the Role rather than a process-global singleton).
    get_cwd: Callable[[], str]
    get_tool_session: Callable[[str], Any]
    set_tool_session: Callable[[str, Any], None]
    # Capability accessor returning the session's SandboxRuntime, or None when no
    # OS-level sandbox is configured. Defaults to a no-runtime stub so a tool
    # bound without a Role (some unit tests) still runs un-sandboxed.
    get_sandbox_runtime: Callable[[], Any] = staticmethod(lambda: None)
    # Capability accessors for session-resume kernel-state restore:
    #   record_kernel_state — persist (cwd, env diff, unset) into the rollout
    #     after a cell settles at idle (so resume can re-seed a kernel).
    #   take_pending_kernel_restore — pop the state staged by resume_session
    #     (or None); applied once when a fresh kernel starts.
    # Both default to no-op stubs so a tool bound without a Role (unit tests)
    # still runs (no recording, no restore).
    record_kernel_state: Callable[..., None] = staticmethod(lambda *a, **k: None)
    take_pending_kernel_restore: Callable[[], Optional[dict]] = staticmethod(lambda: None)

    async def _ensure_session(self) -> KernelSession:
        """Return this Role's live kernel, starting a fresh one if needed.

        The session is stored on RoleState keyed by the tool name; a previously
        stored kernel that has since died is dropped and replaced.
        """
        session = self.get_tool_session(self.name)
        if session is not None and not session.closed:
            return session
        if session is not None:
            session.kill()  # previous kernel died — start fresh
        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None
        runtime = self.get_sandbox_runtime() if self.get_sandbox_runtime is not None else None
        session = KernelSession(session_key=self.session_id, cwd=base_cwd, sandbox_runtime=runtime)
        await session.start()
        # On a resumed session, re-seed the fresh kernel to the saved kernel
        # state (cwd + env diff) without re-running any user code. Consumed once
        # (the accessor clears it), so a subsequent restart starts clean.
        pending = self.take_pending_kernel_restore()
        if pending:
            await session.restore_state(
                pending.get("cwd", ""),
                pending.get("env", {}),
                pending.get("unset", []),
            )
        self.set_tool_session(self.name, session)
        return session

    async def call(
        self,
        *,
        code: str = "",
        interrupt: bool = False,
        restart: bool = False,
        close: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> str:
        """Execute / interrupt / restart / close the session's persistent kernel.

        Args:
            code: Python source to execute in the kernel. State (variables,
                imports, functions) persists across calls. Leave empty when only
                interrupting/restarting/closing.
            interrupt: Send a KeyboardInterrupt to the kernel (e.g. to stop a
                wedged computation). Ignores ``code``.
            restart: Restart the kernel, clearing all in-memory state. Ignores
                ``code``.
            close: Shut the kernel down entirely. Ignores ``code``.
            timeout: Maximum seconds to wait for the code to finish (clamped to
                1..600). On timeout the kernel is interrupted (its state is
                preserved) and whatever it printed so far is returned.
        """
        if close:
            session = self.get_tool_session(self.name)
            if session is None:
                return "[no kernel to close]"
            await session.shutdown()
            self.set_tool_session(self.name, None)
            return "[kernel closed]"

        try:
            if restart:
                session = self.get_tool_session(self.name)
                if session is None or session.closed:
                    await self._ensure_session()  # none live — start a clean one
                else:
                    await session.restart()
                return "[kernel restarted; all variables cleared]"
            if interrupt:
                session = self.get_tool_session(self.name)
                if session is None or session.closed:
                    raise ToolError(_MSG_NO_KERNEL_TO_INTERRUPT)
                text = await session.interrupt()
                return _join(text, "[kernel interrupted]")
            session = await self._ensure_session()
            text, timed_out = await session.execute(code, timeout)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_KERNEL_FAILED.format(error=e))

        if not timed_out:
            # Cell settled at idle — snapshot the kernel env for resume. Skipped
            # on timeout (the kernel was just interrupted and may be recovering),
            # mirroring the terminal capturing only when back at a prompt.
            try:
                state = await session.capture_state()
            except Exception as exc:  # noqa: BLE001 — capture must not break the call
                logger.debug(f"python: kernel state capture failed: {exc}")
                state = None
            if state is not None:
                # getattr-tolerant for tools bound without the capability (tests).
                recorder = getattr(self, "record_kernel_state", None)
                if recorder is not None:
                    recorder(*state, tool=self.name)

        if timed_out:
            return _join(
                text,
                f"[execution timed out after {int(timeout)}s; kernel interrupted " f"— state preserved]",
            )
        return text

    def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's kernel (idempotent)."""
        session = self.get_tool_session(self.name)
        if session is not None:
            session.kill()
            self.set_tool_session(self.name, None)


def _join(text: str, footer: str) -> str:
    """Join output text with a state footer, dropping empty parts."""
    text = text.rstrip("\n")
    if text:
        return f"{text}\n{footer}"
    return footer
