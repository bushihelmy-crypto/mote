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

The live :class:`TerminalSession` is owned by the Role: it is stored on the
Role's ``RoleState`` (via the ``get_tool_session`` / ``set_tool_session``
capabilities) rather than a process-global singleton, so each Role's terminal is
isolated and torn down with it.
"""
from __future__ import annotations

import os
from typing import ClassVar, Optional

from mote.common.logs import logger
from mote.common.schema.permission_types import PermissionDecision
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import (
    GetCwd,
    GetSandboxRuntime,
    GetToolSession,
    RecordTerminalState,
    SetToolSession,
    TakePendingTerminalRestore,
)
from mote.executor.dependency._terminal import DEFAULT_YIELD_MS, TerminalSession
from mote.executor.permission.classifier import classify_command
from mote.executor.permission.command_parse import segment_strings
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_NO_TERMINAL_TO_INTERRUPT = "Error: no live terminal to interrupt."
_MSG_TERMINAL_FAILED = "Error driving terminal: {error}"


@register_tool
class Terminal(BaseTool):
    """Type into a persistent interactive terminal (one per session)."""

    name = "Terminal"
    aliases = ["terminal.run"]
    # Recall synonyms for tool-search: ways a model asks to run shell/commands
    # that the summary ("persistent interactive terminal") does not spell out.
    keywords: ClassVar[list[str]] = [
        "shell",
        "command",
        "bash",
        "cli",
        "console",
        "execute command",
        "命令行",
        "终端",
        "运行命令",
    ]
    max_result_size_chars: ClassVar[int] = 30_000
    requires = (
        "get_cwd",
        "get_tool_session",
        "set_tool_session",
        "get_sandbox_runtime",
        "record_terminal_state",
        "take_pending_terminal_restore",
    )
    # Arbitrary command execution — the highest-risk tool.
    risk_level = "high"
    # Holds a live PTY shell on RoleState between calls.
    stateful = True

    # Injected from Role by bind(): the cwd accessor (seeds the shell's initial
    # directory on first use) + the per-Role tool-session store (where the live
    # TerminalSession is kept, so it persists across calls and is owned by the
    # Role rather than a process-global singleton).
    get_cwd: GetCwd
    get_tool_session: GetToolSession
    set_tool_session: SetToolSession
    # Capability accessor returning the session's SandboxRuntime, or None when no
    # OS-level sandbox is configured. Defaults to a no-runtime stub so a tool
    # bound without a Role (some unit tests) still runs un-sandboxed.
    get_sandbox_runtime: GetSandboxRuntime = staticmethod(lambda: None)
    # Capability accessors for session-resume terminal-state restore:
    #   record_terminal_state — persist (cwd, env diff, unset) into the rollout
    #     after a call settles at a prompt (so resume can re-seed a shell).
    #   take_pending_terminal_restore — pop the state staged by resume_session
    #     (or None); applied once when a fresh shell starts.
    # Both default to no-op stubs so a tool bound without a Role (unit tests)
    # still runs (no recording, no restore).
    record_terminal_state: RecordTerminalState = staticmethod(lambda *a, **k: None)
    take_pending_terminal_restore: TakePendingTerminalRestore = staticmethod(lambda: None)

    async def _ensure_session(self) -> TerminalSession:
        """Return this Role's live terminal, starting a fresh one if needed.

        The session is stored on RoleState keyed by the tool name; a previously
        stored shell that has since exited is dropped and replaced.
        """
        session = self.get_tool_session(self.name)
        if session is not None and not session.closed:
            return session
        if session is not None:
            session.shutdown()  # previous shell exited — start fresh
        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None
        runtime = self.get_sandbox_runtime() if self.get_sandbox_runtime is not None else None
        session = TerminalSession(session_key=self.session_id, cwd=base_cwd, sandbox_runtime=runtime)
        await session.start()
        # On a resumed session, re-seed the fresh shell to the saved terminal
        # state (cwd + env diff) without re-running any user commands. Consumed
        # once (the accessor clears it), so a subsequent restart starts clean.
        pending = self.take_pending_terminal_restore()
        if pending:
            await session.restore_state(
                pending.get("cwd", ""),
                pending.get("env", {}),
                pending.get("unset", []),
            )
        self.set_tool_session(self.name, session)
        return session

    def permission_target(self, args: dict) -> str:
        """The typed input — matched against ``terminal(pattern)`` rules."""
        return args.get("input") or ""

    def permission_segments(self, args: dict) -> "list[str] | None":
        """Split typed input on shell operators for per-segment rule matching.

        Interrupt/close/empty polls carry no command, so they defer (``None``).
        """
        if args.get("interrupt") or args.get("close"):
            return None
        command = args.get("input") or ""
        return segment_strings(command) or None

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
        """Drive a persistent interactive terminal — for REPLs and long-lived sessions.

        Type into a persistent interactive terminal kept alive across calls (one
        per session). State (cwd, env, venv) persists; typing a program like
        'python3' puts it in the foreground so later input is fed to it. Set
        interrupt=true to send Ctrl-C, close=true to shut the terminal down. For
        ordinary one-shot commands prefer the Bash tool.

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
            session = self.get_tool_session(self.name)
            if session is None:
                return "[no terminal to close]"
            session.shutdown()
            self.set_tool_session(self.name, None)
            return "[terminal closed]"

        result: tuple[str, Optional[int], bool, bool]
        try:
            if interrupt:
                session = self.get_tool_session(self.name)
                if session is None or session.closed:
                    raise ToolError(_MSG_NO_TERMINAL_TO_INTERRUPT)
                result = await session.interrupt(yield_time_ms)
            else:
                session = await self._ensure_session()
                result = await session.feed(input, yield_time_ms)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_TERMINAL_FAILED.format(error=e))

        if result[3]:  # the shell itself exited — drop the stored session
            session.shutdown()
            self.set_tool_session(self.name, None)
        elif result[2]:  # at_prompt and still alive — snapshot the env for resume
            try:
                state = await session.capture_state()
            except Exception as exc:  # noqa: BLE001 — capture must not break the call
                logger.debug(f"terminal: session state capture failed: {exc}")
                state = None
            if state is not None:
                # getattr-tolerant for tools bound without the capability (tests).
                recorder = getattr(self, "record_terminal_state", None)
                if recorder is not None:
                    recorder(*state, tool=self.name)
        return _format_output(*result)

    def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's terminal (idempotent)."""
        session = self.get_tool_session(self.name)
        if session is not None:
            session.shutdown()
            self.set_tool_session(self.name, None)


def _format_output(text: str, exit_code: Optional[int], at_prompt: bool, closed: bool) -> str:
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
