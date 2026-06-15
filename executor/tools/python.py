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

Live state lives in the shared :data:`KERNELS` engine, keyed by the Role session.
"""
from __future__ import annotations

import os
from typing import Callable, ClassVar

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.dependency._kernel import DEFAULT_TIMEOUT_S, KERNELS
from metagpt.common.prompt.tools import PYTHON_DESCRIPTION


@register_tool
class Python(BaseTool):
    """Execute code in a persistent Python kernel (one per session)."""

    name = "Jupyter"
    aliases = ["Python"]
    max_result_size_chars: ClassVar[int] = 30_000
    description = PYTHON_DESCRIPTION
    requires = ("get_cwd",)
    # Arbitrary code execution.
    risk_level = "high"

    # Injected from Role by bind() — only the cwd accessor (seeds the kernel's
    # initial working directory on first use).
    get_cwd: Callable[[], str]

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
            existed = await KERNELS.close(self.session_id)
            return "[kernel closed]" if existed else "[no kernel to close]"

        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None

        try:
            if restart:
                await KERNELS.restart(self.session_id, base_cwd)
                return "[kernel restarted; all variables cleared]"
            if interrupt:
                text = await KERNELS.interrupt(self.session_id)
                return _join(text, "[kernel interrupted]")
            text, timed_out = await KERNELS.execute(
                self.session_id, base_cwd, code, timeout
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Error running Python kernel: {e}")

        if timed_out:
            return _join(
                text,
                f"[execution timed out after {int(timeout)}s; kernel interrupted "
                f"— state preserved]",
            )
        return text

    def cleanup_session(self, session_id: str) -> None:
        """Tear down this session's kernel."""
        KERNELS.cleanup_session(session_id)


def _join(text: str, footer: str) -> str:
    """Join output text with a state footer, dropping empty parts."""
    text = text.rstrip("\n")
    if text:
        return f"{text}\n{footer}"
    return footer
