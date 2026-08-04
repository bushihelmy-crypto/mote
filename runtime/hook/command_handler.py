"""Governed external Hook argv and its bounded JSON wire contract."""

from __future__ import annotations

import json
import os
from typing import Protocol

from mote.contracts.hook import HookInvocation
from mote.runtime.config.hook import HookCommandHandler
from mote.runtime.hook.parser import parse_command_output
from mote.runtime.hook.types import HookOutcome
from mote.runtime.hook.wire import HookWireSerializer
from mote.runtime.process import (
    FixedProcessEnvironment,
    ProcessDisposition,
    resolve_fixed_executable,
    run_verified_fixed_argv,
)

DEFAULT_TIMEOUT = 60.0
MAX_HOOK_OUTPUT_BYTES = 256 * 1024


class HookCommandFailure(RuntimeError):
    """A command did not produce a valid authoritative Hook outcome."""


class HookCommandSandbox(Protocol):
    async def wrap_exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        extra_writable: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]: ...


async def run_command_handler(
    cfg: HookCommandHandler,
    hook_input: HookInvocation,
    *,
    sandbox: HookCommandSandbox | None,
    timeout: float | None = None,
) -> HookOutcome:
    """Execute one approved structured argv without parent environment access."""
    effective_timeout = timeout if timeout is not None else cfg.timeout
    effective_timeout = effective_timeout or DEFAULT_TIMEOUT
    payload = json.dumps(HookWireSerializer().to_json_dict(hook_input)) + "\n"
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL") if key in os.environ}
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = "/tmp"
    env["TMPDIR"] = "/tmp"
    if sandbox is None:
        raise HookCommandFailure("Hook command sandbox is unavailable")
    try:
        wrapped_argv, wrapped_env = await sandbox.wrap_exec(
            list(cfg.argv),
            cwd=hook_input.identity.cwd or None,
            env=env,
        )
    except Exception as exc:
        raise HookCommandFailure("Hook command sandbox activation failed") from exc
    result = await run_verified_fixed_argv(
        resolve_fixed_executable(wrapped_argv[0]),
        wrapped_argv[1:],
        working_dir=hook_input.identity.cwd or None,
        env=FixedProcessEnvironment.compile(wrapped_env),
        timeout=effective_timeout,
        max_output_bytes=MAX_HOOK_OUTPUT_BYTES,
        stdin=payload.encode("utf-8"),
    )
    if result.disposition is not ProcessDisposition.EXITED:
        raise HookCommandFailure(f"hook process disposition: {result.disposition.value}")
    if result.exit_code not in (0, 2):
        raise HookCommandFailure(f"hook process exited with status {result.exit_code}")
    return parse_command_output(
        result.stdout,
        result.stderr,
        result.exit_code or 0,
        strict=True,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "HookCommandFailure",
    "HookCommandSandbox",
    "MAX_HOOK_OUTPUT_BYTES",
    "run_command_handler",
]
