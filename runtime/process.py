"""Asynchronous child-process execution owned by Runtime."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

EXEC_TIMEOUT_EXIT_CODE = 124


async def aexecute(
    cmd: str,
    working_dir: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = True,
    timeout: float | None = None,
    check: bool = False,
    wait: bool = False,
    return_partial_on_timeout: bool = False,
    sandbox_runtime: Any | None = None,
):
    """Execute a command, optionally collecting its output and timeout state."""
    if sandbox_runtime is not None:
        cmd, env = await sandbox_runtime.wrap_command(cmd, cwd=working_dir, env=env)
    if shell:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=working_dir,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=working_dir,
        )
    if not wait:
        return None
    if return_partial_on_timeout:
        return await _capture_partial(process, cmd, timeout, check)
    stdout_buffer, stderr_buffer = bytearray(), bytearray()
    try:
        await asyncio.wait_for(_collect(process, stdout_buffer, stderr_buffer), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _terminate(process)
        raise asyncio.TimeoutError(f"Command '{cmd}' timed out after {timeout} seconds") from exc
    result = (
        process.returncode or 0,
        _decode(stdout_buffer),
        _decode(stderr_buffer),
    )
    if check and result[0] != 0:
        raise RuntimeError(_failure(cmd, *result))
    return result


async def _capture_partial(process, cmd: str, timeout: float | None, check: bool):
    stdout_buffer, stderr_buffer = bytearray(), bytearray()
    timed_out = False
    try:
        await asyncio.wait_for(_collect(process, stdout_buffer, stderr_buffer), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate(process)
    code = EXEC_TIMEOUT_EXIT_CODE if timed_out else (process.returncode if process.returncode is not None else -1)
    result = (code, _decode(stdout_buffer), _decode(stderr_buffer), timed_out)
    if check and not timed_out and code != 0:
        raise RuntimeError(_failure(cmd, code, result[1], result[2]))
    return result


async def _collect(process, stdout_buffer: bytearray, stderr_buffer: bytearray) -> None:
    async def drain(stream, buffer: bytearray) -> None:
        while stream is not None and (chunk := await stream.read(8192)):
            buffer.extend(chunk)

    await asyncio.gather(
        drain(process.stdout, stdout_buffer),
        drain(process.stderr, stderr_buffer),
    )
    await _observe_returncode(process)


async def _observe_returncode(process) -> None:
    while process.returncode is None:
        await asyncio.sleep(0)


async def _terminate(process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(_observe_returncode(process), timeout=2.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_observe_returncode(process), timeout=2.0)


def _decode(value) -> str:
    return bytes(value or b"").decode(errors="replace").strip()


def _failure(cmd: str, code: int, stdout: str, stderr: str) -> str:
    return f"Command '{cmd}' failed with return code {code}\nSTDOUT: {stdout}\nSTDERR: {stderr}"


__all__ = ["EXEC_TIMEOUT_EXIT_CODE", "aexecute"]
