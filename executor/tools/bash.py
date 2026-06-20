"""Bash command tool — aligned with Claude Code's Bash tool.

Runs a shell command in the role's current working directory and keeps the
live cwd in sync: after each command we probe `pwd` so that a `cd` inside the
command persists to the next turn, the same way Claude Code calls setCwd()
after resolving the shell's working directory.

aexecute() spawns a fresh subprocess per call (no persistent shell), so a bare
`cd` would not survive across calls. We emulate persistence by always running
in the role's cwd and writing the probed directory back via set_cwd.

Cwd ownership stays in the Role. This tool only borrows two narrow accessors
(get_cwd / set_cwd) injected by bind(); it never sees RoleState or memory.
"""
from __future__ import annotations

import os
from typing import Callable, ClassVar

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.permission.classifier import classify_command
from metagpt.executor.permission.command_parse import segment_strings
from metagpt.common.schema.permission_types import PermissionDecision
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.common.utils.common import aexecute
from metagpt.common.prompt.tools import BASH_DESCRIPTION

# Unique marker so we can split the command's real output from the trailing
# pwd probe without clashing with normal output.
_CWD_MARKER = "__METAGPT_CWD__:"


@register_tool
class Bash(BaseTool):
    """Run a bash command in the current working directory and return its output."""

    name = "Bash"
    aliases = ["Bash.run", "bash"]
    # Shell output can be verbose; cap below the default (CC).
    max_result_size_chars: ClassVar[int] = 30_000
    description = BASH_DESCRIPTION
    requires = ("get_cwd", "set_cwd")
    # Arbitrary command execution — the highest-risk tool.
    risk_level = "high"

    # Injected from Role by bind() — only these two cwd accessors, never RoleState
    # or memory.
    get_cwd: Callable[[], str]
    set_cwd: Callable[[str], None]

    def permission_target(self, args: dict) -> str:
        """The command string — matched against ``Bash(pattern)`` rules."""
        return args.get("command") or ""

    def permission_segments(self, args: dict) -> "list[str] | None":
        """Split the command on shell operators for per-segment rule matching."""
        command = args.get("command") or ""
        return segment_strings(command) or None

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        """Classify the command and short-circuit the obvious cases.

        Uses the shared :func:`classify_command` (Codex-style safety pre-check):

          * destructive (``rm -rf``, ``mkfs``, ``sudo`` ...) -> ``ask``
            (bypass-immune — forces a prompt regardless of allow rules / mode);
          * verifiably read-only (``ls``, ``cat``, ``git status`` ...) ->
            ``allow`` (auto-approved, no prompt in ``default`` mode);
          * anything unrecognised -> ``None`` to defer to rules/mode.
        """
        command = args.get("command") or ""
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

    async def call(self, *, command: str, timeout: float = 300.0, workdir: str = "") -> str:
        """Execute a bash command in the session's current working directory.

        Args:
            command: The bash command to execute.
            timeout: Maximum seconds to wait for the command (default 300). On
                timeout the command is terminated and whatever output it produced
                so far is returned (rather than raising), mirroring codex.
            workdir: Optional directory to run this one command in. Relative paths
                resolve against the session's current working directory. Prefer
                this over a leading ``cd``. Using ``workdir`` is transient — it
                does NOT change the session's persistent cwd; omit it and use a
                ``cd`` if you want the directory change to stick across calls.
        """
        if not command or not command.strip():
            raise ToolError("Error: 'command' argument is required.")

        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None

        # `workdir` is a transient per-call override: resolve it (relative to the
        # persistent cwd) and run there WITHOUT persisting the result back. When
        # omitted we run in the persistent cwd and persist any `cd` the command
        # makes (probed below).
        if workdir:
            run_cwd = workdir if os.path.isabs(workdir) else os.path.join(base_cwd or os.getcwd(), workdir)
            if not os.path.isdir(run_cwd):
                raise ToolError(f"Error: workdir does not exist: {workdir}")
            persist_cwd = False
        else:
            run_cwd = base_cwd  # may be None -> aexecute uses the process default
            persist_cwd = True

        # Append a probe so we can capture the command's real exit code and the
        # directory it ended in (e.g. after a `cd`), then persist cwd back via
        # set_cwd. `$?` here reflects the user command, since the probe runs
        # immediately after it.
        probe = f'echo "{_CWD_MARKER}$?:$(pwd)"'
        wrapped = f"{command}\n{probe}"

        try:
            _rc, stdout, stderr, timed_out = await aexecute(
                wrapped, working_dir=run_cwd, wait=True, timeout=timeout, return_partial_on_timeout=True
            )
        except Exception as e:
            raise ToolError(f"Error executing command: {e}")

        output, rc, new_cwd = self._split_probe(stdout)
        # On timeout the probe never ran, so the marker is absent and cwd cannot
        # be trusted; skip persistence. Otherwise persist the probed cwd.
        if persist_cwd and not timed_out and new_cwd and os.path.isdir(new_cwd):
            self.set_cwd(new_cwd)

        parts = []
        if timed_out:
            parts.append(f"command timed out after {int(timeout * 1000)} milliseconds")
        if output:
            parts.append(output)
        if stderr:
            parts.append(stderr)
        if not timed_out and rc:
            parts.append(f"[exit code: {rc}]")
        return "\n".join(parts) if parts else ""

    @staticmethod
    def _split_probe(stdout: str) -> tuple[str, int, str]:
        """Split command output from the trailing probe marker.

        The probe is ``__METAGPT_CWD__:<exit_code>:<cwd>``. Returns
        (output_without_probe, exit_code, probed_cwd). If the marker is missing
        (e.g. the command crashed before the probe ran), returns (stdout, 0, "").
        """
        if not stdout:
            return "", 0, ""
        idx = stdout.rfind(_CWD_MARKER)
        if idx < 0:
            return stdout, 0, ""
        output = stdout[:idx].rstrip("\n")
        tail = stdout[idx + len(_CWD_MARKER):].strip()
        code_str, _, new_cwd = tail.partition(":")
        try:
            rc = int(code_str)
        except ValueError:
            rc = 0
        return output, rc, new_cwd

    def cleanup_session(self, session_id: str) -> None:
        """No persistent process to tear down; aexecute spawns per-call."""
        pass
