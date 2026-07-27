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

The PTY is a managed runtime owned by the Role's ``RuntimeHost``. Calls acquire
serialized, fenced write access; the host owns identity, revision and teardown.
"""
from __future__ import annotations

import os
from typing import ClassVar, Optional

from mote.contracts.errors.runtimes import ManagedRuntimeNotFoundError
from mote.contracts.permissions import PermissionDecision
from mote.contracts.runtimes import RuntimeAccessMode
from mote.product.toolsets.builtin.runtime_action import handoff_permission, is_handoff_action, run_handoff_action
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import GetCwd, GetRuntimeHost, GetSandboxRuntime, HandoffRuntime
from mote.runtime.tools.dependency._terminal import DEFAULT_YIELD_MS, TerminalRuntimeDriver
from mote.runtime.tools.permission.classifier import classify_command
from mote.runtime.tools.permission.command_parse import segment_strings
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_NO_TERMINAL_TO_INTERRUPT = "Error: no live terminal to interrupt."
_MSG_TERMINAL_FAILED = "Error driving terminal: {error}"
_MSG_UNKNOWN_ACTION = "Error: unknown terminal action '{action}'. Use handoff or leave action empty."
_RUNTIME = "terminal:default"


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
        "get_runtime_host",
        "get_sandbox_runtime",
        "handoff_runtime",
    )
    # Arbitrary command execution — the highest-risk tool.
    risk_level = "high"
    # Fronts a live PTY managed by RuntimeHost between calls.
    stateful = True

    # Injected from Role by bind(): cwd seeds the shell and RuntimeHost owns it.
    get_cwd: GetCwd
    get_runtime_host: GetRuntimeHost
    handoff_runtime: HandoffRuntime
    # Capability accessor returning the session's SandboxRuntime, or None when no
    # OS-level sandbox is configured. Defaults to a no-runtime stub so a tool
    # bound without a Role (some unit tests) still runs un-sandboxed.
    get_sandbox_runtime: GetSandboxRuntime = staticmethod(lambda: None)

    async def _ensure_runtime(self) -> None:
        """Atomically create this Role's implicit terminal runtime when absent."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
            return
        except ManagedRuntimeNotFoundError:
            pass
        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None
        runtime = self.get_sandbox_runtime() if self.get_sandbox_runtime is not None else None
        driver = TerminalRuntimeDriver(
            session_key=self.session_id,
            cwd=base_cwd,
            sandbox_runtime=runtime,
        )
        await host.ensure(driver)

    def permission_target(self, args: dict) -> str:
        """The typed input — matched against ``terminal(pattern)`` rules."""
        if is_handoff_action(args):
            return ""
        return args.get("input") or ""

    def permission_segments(self, args: dict) -> "list[str] | None":
        """Split typed input on shell operators for per-segment rule matching.

        Interrupt/close/empty polls carry no command, so they defer (``None``).
        """
        if is_handoff_action(args) or args.get("interrupt") or args.get("close"):
            return None
        command = args.get("input") or ""
        return segment_strings(command) or None

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        """Classify the input (same Codex-style pre-check as :class:`Bash`).

        destructive -> bypass-immune ``ask``; verifiably read-only -> ``allow``;
        anything unrecognised -> ``None`` (defer to rules/mode). Interrupt/close
        and empty polls carry no command, so they defer.
        """
        if is_handoff_action(args):
            return handoff_permission()
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
        action: str = "",
        input: str = "",
        interrupt: bool = False,
        close: bool = False,
        yield_time_ms: int = DEFAULT_YIELD_MS,
        message: str = "",
    ) -> str | ToolResult:
        """Drive a persistent interactive terminal — for REPLs and long-lived sessions.

        Type into a persistent interactive terminal kept alive across calls (one
        per session). State (cwd, env, venv) persists; typing a program like
        'python3' puts it in the foreground so later input is fed to it. Set
        interrupt=true to send Ctrl-C, close=true to shut the terminal down.
        Use action=handoff to give the user exclusive control of an already-open
        terminal and wait until control returns.

        Args:
            action: Set to handoff for direct user interaction with the live
                terminal; otherwise leave empty.
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
            message: Optional instructions shown to the user during handoff.
        """
        action = (action or "").strip().lower()
        if action == "handoff":
            return await run_handoff_action(self.handoff_runtime, _RUNTIME, message=message)
        if action:
            raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))
        host = self.get_runtime_host()
        if close:
            try:
                host.descriptor(_RUNTIME)
            except ManagedRuntimeNotFoundError:
                return "[no terminal to close]"
            await host.close(_RUNTIME)
            return "[terminal closed]"

        result: tuple[str, Optional[int], bool, bool]
        try:
            if interrupt:
                try:
                    host.descriptor(_RUNTIME)
                except ManagedRuntimeNotFoundError:
                    raise ToolError(_MSG_NO_TERMINAL_TO_INTERRUPT)
            else:
                await self._ensure_runtime()
            async with host.access(
                _RUNTIME,
                mode=RuntimeAccessMode.WRITE,
                owner_id=f"agent:{self.session_id}:terminal",
            ) as access:
                driver = access.driver
                if not isinstance(driver, TerminalRuntimeDriver):
                    raise RuntimeError("terminal runtime has an unexpected driver")
                if interrupt:
                    if driver.closed:
                        raise ToolError(_MSG_NO_TERMINAL_TO_INTERRUPT)
                    result = await driver.interrupt(yield_time_ms)
                else:
                    result = await driver.feed(input, yield_time_ms)
                text, exit_code, at_prompt, closed = result
                access.commit(
                    changed=bool(interrupt or input or text or exit_code is not None or closed or not at_prompt)
                )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_TERMINAL_FAILED.format(error=e))

        if result[3]:  # the shell itself exited — retire the managed runtime
            await host.close(_RUNTIME)
        return _format_output(*result)

    async def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's terminal (idempotent)."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            return
        await host.close(_RUNTIME)


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
