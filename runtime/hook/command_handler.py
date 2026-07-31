"""External command hook handler — the JSON stdin/stdout contract.

Spawns the configured shell command, writes the hook input as a single JSON
line on stdin (newline-terminated so a ``read -r`` in a shell script works),
captures stdout/stderr, enforces a per-handler timeout (killing the process on
expiry), and injects a couple of environment variables for convenience.

Best-effort by contract: any spawn / timeout / decode failure is logged as a
warning and turns into :data:`HookOutcome.EMPTY` — a misbehaving hook must never
break the agent's turn.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mote.contracts.hook import HookInvocation
from mote.runtime.hook.parser import parse_command_output
from mote.runtime.hook.types import EMPTY, HookOutcome
from mote.runtime.hook.wire import HookWireSerializer
from mote.runtime.telemetry.logging import logger

# Fallback timeout (seconds) when neither the handler nor the caller specifies
# one — a 60s default hook budget.
DEFAULT_TIMEOUT = 60.0


async def run_command_handler(
    cfg: Any,
    hook_input: HookInvocation,
    *,
    timeout: float | None = None,
) -> HookOutcome:
    """Run one external command handler and parse its output.

    Args:
        cfg: A ``HookCommandHandler`` (duck-typed: needs ``.command`` and
            optionally ``.timeout``).
        hook_input: The event payload; serialized to JSON on the child's stdin.
        timeout: Overall timeout override; falls back to ``cfg.timeout`` then
            :data:`DEFAULT_TIMEOUT`.

    Returns:
        The parsed :class:`HookOutcome`, or ``EMPTY`` on any failure.
    """
    command = getattr(cfg, "command", None)
    if not command:
        return EMPTY

    effective_timeout = timeout
    if effective_timeout is None:
        effective_timeout = getattr(cfg, "timeout", None)
    if effective_timeout is None:
        effective_timeout = DEFAULT_TIMEOUT

    payload = json.dumps(HookWireSerializer().to_json_dict(hook_input)) + "\n"

    env = dict(os.environ)
    if hook_input.identity.cwd:
        env["AGENT_PROJECT_DIR"] = hook_input.identity.cwd
    if hook_input.identity.session_id:
        env["AGENT_SESSION_ID"] = hook_input.identity.session_id

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=hook_input.identity.cwd or None,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=payload.encode("utf-8")),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"hook: command handler timed out after {effective_timeout}s: {command!r}")
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.debug(f"hook: killing timed-out command handler failed: {exc}")
        return EMPTY
    except Exception as exc:  # noqa: BLE001 — any spawn failure is non-fatal
        logger.warning(f"hook: command handler failed to run {command!r}: {exc}")
        return EMPTY

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return parse_command_output(stdout, stderr, proc.returncode or 0)


__all__ = ["run_command_handler", "DEFAULT_TIMEOUT"]
