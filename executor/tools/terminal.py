"""terminal — one persistent interactive terminal the model types into.

A single tool that merges what codex splits into ``exec_command`` + ``write_stdin``:
there is **one implicit terminal per Role session** (no session id to track, like a
Jupyter kernel), PTY-backed, that the model drives by typing:

- ``input`` is typed into the terminal (a newline is added if absent). At a shell
  prompt it runs a command; when a program is in the foreground (e.g. ``python3``)
  it is fed to that program's stdin. ``cd`` / ``export`` / venv activation persist.
- ``interrupt=True`` sends Ctrl-C to the foreground program (reclaim a hung shell).
- ``close=True`` tears the whole terminal down.

Each call returns whatever printed within a short yield window. If the terminal is
back at a prompt the command's exit code is reported; if a foreground program is
still running you get the output so far and a note to send more input / interrupt /
close. Use the one-shot :class:`Bash` tool for ordinary "run and get the result"
commands; use this when you need persistent shell state or to drive a program
interactively.

Live state lives in the shared :data:`TERMINAL` engine, keyed by the Role session.
"""
from __future__ import annotations

import os
from typing import Callable, ClassVar, Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.permission.classifier import classify_command
from metagpt.executor.permission.types import PermissionDecision
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.dependency._terminal import DEFAULT_YIELD_MS, TERMINAL
from metagpt.common.prompt.tools import TERMINAL_DESCRIPTION


@register_tool
class Terminal(BaseTool):
    """Type into a persistent interactive terminal (one per session)."""

    name = "Terminal"
    aliases = ["terminal.run"]
    max_result_size_chars: ClassVar[int] = 30_000
    description = TERMINAL_DESCRIPTION
    requires = ("get_cwd",)
    # Arbitrary command execution — the highest-risk tool.
    risk_level = "high"

    # Injected from Role by bind() — only the cwd accessor (seeds the shell's
    # initial directory on first use).
    get_cwd: Callable[[], str]

    def permission_target(self, args: dict) -> str:
        """The typed input — matched against ``terminal(pattern)`` rules."""
        return args.get("input") or ""

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        """Classify the input (same Codex-style pre-check as :class:`Bash`).

        destructive -> bypass-immune ``ask``; verifiably read-only -> ``allow``;
        anything unrecognised -> ``None`` (defer to rules/mode). Interrupt/close
        and empty polls carry no command, so they defer.
        """
        if args.get("interrupt") or args.get("close"):
            return None
        command = args.get("input") or ""
        if not command.strip():
            return None
        assessment = classify_command(command)
        if assessment.risk == "high":
            return PermissionDecision.ask(
                "tool_check",
                "potentially destructive command",
                message=f"This command looks destructive: {command}",
            )
        if assessment.known_safe:
            return PermissionDecision.allow("tool_check", assessment.reason)
        return None

    async def call(
        self,
        *,
        input: str = "",
        interrupt: bool = False,
        close: bool = False,
        yield_time_ms: int = DEFAULT_YIELD_MS,
    ) -> str:
        """Type into / interrupt / close the session's persistent terminal.

        Args:
            input: Text to type into the terminal. At a shell prompt this runs a
                command (a trailing newline is added if absent); when a program is
                in the foreground it is fed to that program's stdin. Leave empty to
                just poll for more output.
            interrupt: Send Ctrl-C to the foreground program (e.g. to stop a hung
                command and return to the shell). Ignores ``input``.
            close: Shut the terminal down entirely (kills the shell and any
                foreground program). Ignores ``input``.
            yield_time_ms: How long to wait for output before returning (clamped to
                250..60000ms). If the shell returns to a prompt within this window
                you get the command's output + exit code; otherwise you get the
                output so far and the terminal stays busy with a foreground program.
        """
        if close:
            existed = TERMINAL.close(self.session_id)
            return "[terminal closed]" if existed else "[no terminal to close]"

        try:
            if interrupt:
                result = await TERMINAL.interrupt(self.session_id, yield_time_ms)
            else:
                cwd = self.get_cwd()
                base_cwd = cwd if cwd and os.path.isdir(cwd) else None
                result = await TERMINAL.interact(
                    self.session_id, base_cwd, input, yield_time_ms
                )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Error driving terminal: {e}")

        return _format_output(*result)

    def cleanup_session(self, session_id: str) -> None:
        """Tear down this session's terminal."""
        TERMINAL.cleanup_session(session_id)


def _format_output(
    text: str, exit_code: Optional[int], at_prompt: bool, closed: bool
) -> str:
    """Append a footer describing the terminal's state after this call."""
    parts = []
    if text:
        parts.append(text)
    if closed:
        code = f" (exit code {exit_code})" if exit_code else ""
        parts.append(f"[terminal exited{code}]")
    elif at_prompt:
        if exit_code:
            parts.append(f"[exit code: {exit_code}]")
    else:
        parts.append(
            "[still running; a program holds the terminal — send more input, "
            "interrupt=true to Ctrl-C, or close=true]"
        )
    return "\n".join(parts) if parts else ""
