from __future__ import annotations

import asyncio
import os
import sys

import pytest

import mote.runtime.process as process_module
from mote.runtime.process import (
    AuthorizedShellIntent,
    FixedExecutableBinding,
    ProcessDisposition,
    ProcessResult,
    run_authorized_shell,
    run_fixed_argv,
    run_verified_fixed_argv,
)
from mote.runtime.tools.execution_context import AuthorizedToolInvocation, bind_authorized_invocation


@pytest.mark.asyncio
async def test_verified_fixed_argv_executes_admitted_inode_and_rejects_replacement(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "helper"
    executable.write_text("#!/bin/sh\nprintf admitted", encoding="utf-8")
    executable.chmod(0o700)
    metadata = executable.stat()
    binding = FixedExecutableBinding(str(executable), metadata.st_dev, metadata.st_ino)

    observed = {}

    async def spawn(argv, **kwargs):
        descriptor = kwargs["pass_fds"][0]
        observed["identity"] = (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        observed["argv"] = argv
        return ProcessResult(ProcessDisposition.EXITED, stdout="admitted", exit_code=0)

    monkeypatch.setattr(process_module, "_spawn_and_collect", spawn)
    result = await run_verified_fixed_argv(binding, ())
    assert result.stdout == "admitted"
    assert observed["identity"] == (metadata.st_dev, metadata.st_ino)
    assert observed["argv"][0].startswith("/proc/self/fd/")

    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\nprintf replaced", encoding="utf-8")
    replacement.chmod(0o700)
    replacement.replace(executable)
    with pytest.raises(PermissionError):
        await run_verified_fixed_argv(binding, ())


class _PassthroughSandbox:
    async def wrap_command(self, command: str, *, cwd=None, env=None):
        return command, dict(env or {})


@pytest.mark.asyncio
async def test_fixed_argv_does_not_expand_shell_syntax(tmp_path) -> None:
    marker = tmp_path / "must-not-exist"
    result = await run_fixed_argv((sys.executable, "-c", "import sys; print(sys.argv[1])", f"> {marker}"))

    assert result.disposition is ProcessDisposition.EXITED
    assert result.stdout == f"> {marker}"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_fixed_argv_enforces_output_bound() -> None:
    result = await run_fixed_argv(
        (sys.executable, "-c", "print('x' * 10000)"),
        max_output_bytes=32,
    )

    assert result.disposition is ProcessDisposition.OUTPUT_LIMIT
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_fixed_argv_invalid_utf8_is_typed() -> None:
    result = await run_fixed_argv(
        (sys.executable, "-c", "import os; os.write(1, b'\\xff')"),
        max_output_bytes=32,
    )

    assert result.disposition is ProcessDisposition.OUTPUT_DECODE_FAILED
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_fast_output_process_does_not_stall_after_exit() -> None:
    payload_size = 50_000
    script = f"import sys; sys.stdout.write('x' * {payload_size})"

    result = await asyncio.wait_for(
        run_fixed_argv((sys.executable, "-c", script), timeout=1.0),
        timeout=2.0,
    )

    assert result.disposition is ProcessDisposition.EXITED
    assert result.exit_code == 0
    assert len(result.stdout) == payload_size


@pytest.mark.asyncio
async def test_shell_timeout_is_typed_and_keeps_partial_output() -> None:
    command = "echo started; sleep 10"
    with bind_authorized_invocation(AuthorizedToolInvocation("Bash", {"command": command}, 1)):
        result = await run_authorized_shell(
            AuthorizedShellIntent(command, "Bash", authorization_generation=1),
            sandbox=_PassthroughSandbox(),
            timeout=0.05,
        )

    assert result.disposition is ProcessDisposition.TIMED_OUT
    assert result.stdout == "started"


@pytest.mark.asyncio
async def test_user_shell_requires_sandbox() -> None:
    command = "echo unsafe"
    with bind_authorized_invocation(AuthorizedToolInvocation("Bash", {"command": command}, 1)):
        result = await run_authorized_shell(
            AuthorizedShellIntent(command, "Bash", authorization_generation=1),
            sandbox=None,
        )

    assert result.disposition is ProcessDisposition.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_stale_shell_authorization_is_rejected() -> None:
    with bind_authorized_invocation(AuthorizedToolInvocation("Bash", {"command": "echo old"}, 2)):
        with pytest.raises(PermissionError, match="active authorization"):
            await run_authorized_shell(
                AuthorizedShellIntent("echo old", "Bash", authorization_generation=1),
                sandbox=_PassthroughSandbox(),
            )
