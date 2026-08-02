"""Typed one-shot child-process runners owned by Runtime.

Fixed internal programs and user-authored shell commands deliberately have
different entry points.  Neither API has a ``shell`` switch and neither is a
long-running process lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from mote.runtime.tools.execution_context import current_authorized_invocation


class ProcessDisposition(str, Enum):
    EXITED = "exited"
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    OUTPUT_LIMIT = "output_limit"
    OUTPUT_DECODE_FAILED = "output_decode_failed"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    disposition: ProcessDisposition
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    signal: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AuthorizedShellIntent:
    """A user command proven to match the active Tool authorization."""

    command: str
    tool_name: str
    authorization_generation: int


class UserCommandSandbox(Protocol):
    async def wrap_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]: ...


@dataclass(frozen=True, slots=True)
class FixedExecutableBinding:
    path: str
    device: int
    inode: int


async def run_verified_fixed_argv(
    executable: FixedExecutableBinding,
    arguments: Sequence[str],
    *,
    working_dir: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    max_output_bytes: int | None = None,
) -> ProcessResult:
    """Execute the exact filesystem object admitted by Product policy."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(executable.path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) != (executable.device, executable.inode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o111 == 0
        ):
            raise PermissionError("fixed executable binding no longer matches admission")
        argv = (f"/proc/self/fd/{descriptor}", *tuple(arguments))
        return await _spawn_and_collect(
            argv,
            shell_command=None,
            working_dir=working_dir,
            env=dict(env) if env is not None else None,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            strict_decode=True,
            pass_fds=(descriptor,),
        )
    finally:
        os.close(descriptor)


async def run_fixed_argv(
    argv: Sequence[str],
    *,
    working_dir: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    max_output_bytes: int | None = None,
    stdin: bytes | None = None,
) -> ProcessResult:
    """Run a trusted internal argv without shell parsing or expansion."""
    sealed_argv = tuple(argv)
    if not sealed_argv or any(not isinstance(item, str) or not item for item in sealed_argv):
        raise ValueError("fixed argv must contain one or more non-empty strings")
    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    return await _spawn_and_collect(
        sealed_argv,
        shell_command=None,
        working_dir=working_dir,
        env=dict(env) if env is not None else None,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        strict_decode=True,
        stdin=stdin,
    )


async def run_authorized_shell(
    intent: AuthorizedShellIntent,
    *,
    sandbox: UserCommandSandbox | None,
    working_dir: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> ProcessResult:
    """Run one authorized user shell command through the required sandbox."""
    authorization = current_authorized_invocation()
    authorized_command = authorization.arguments.get("command") if authorization is not None else None
    if (
        authorization is None
        or authorization.tool_name != intent.tool_name
        or authorization.generation != intent.authorization_generation
        or not isinstance(authorized_command, str)
        or authorized_command != intent.command
    ):
        raise PermissionError("user shell intent does not match the active authorization")
    if sandbox is None:
        return ProcessResult(
            ProcessDisposition.SANDBOX_UNAVAILABLE,
            detail="user command sandbox is unavailable",
        )
    try:
        command, wrapped_env = await sandbox.wrap_command(
            intent.command,
            cwd=working_dir,
            env=dict(env) if env is not None else None,
        )
    except Exception as exc:  # sandbox activation is an execution disposition
        return ProcessResult(
            ProcessDisposition.SANDBOX_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return await _spawn_and_collect(
        (),
        shell_command=command,
        working_dir=working_dir,
        env=wrapped_env,
        timeout=timeout,
        max_output_bytes=None,
        strict_decode=False,
    )


async def _spawn_and_collect(
    argv: tuple[str, ...],
    *,
    shell_command: str | None,
    working_dir: str | None,
    env: dict[str, str] | None,
    timeout: float | None,
    max_output_bytes: int | None,
    strict_decode: bool,
    stdin: bytes | None = None,
    pass_fds: tuple[int, ...] = (),
) -> ProcessResult:
    try:
        if shell_command is None:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=working_dir,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                shell_command,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=working_dir,
                start_new_session=True,
            )
    except (OSError, ValueError) as exc:
        return ProcessResult(ProcessDisposition.SPAWN_FAILED, detail=f"{type(exc).__name__}: {exc}")

    stdout_buffer, stderr_buffer = bytearray(), bytearray()
    try:
        await asyncio.wait_for(
            _collect(
                process,
                stdout_buffer,
                stderr_buffer,
                max_output_bytes=max_output_bytes,
                stdin=stdin,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _terminate(process)
        return ProcessResult(
            ProcessDisposition.TIMED_OUT,
            stdout=_decode(stdout_buffer, strict=False),
            stderr=_decode(stderr_buffer, strict=False),
        )
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except _OutputLimitExceeded:
        await _terminate(process)
        return ProcessResult(
            ProcessDisposition.OUTPUT_LIMIT,
            detail=f"process output exceeded {max_output_bytes} bytes",
        )

    returncode = process.returncode if process.returncode is not None else 0
    try:
        stdout = _decode(stdout_buffer, strict=strict_decode)
        stderr = _decode(stderr_buffer, strict=strict_decode)
    except UnicodeDecodeError:
        return ProcessResult(
            ProcessDisposition.OUTPUT_DECODE_FAILED,
            detail="process output is not valid UTF-8",
        )
    if returncode < 0:
        return ProcessResult(
            ProcessDisposition.SIGNALED,
            stdout=stdout,
            stderr=stderr,
            signal=-returncode,
        )
    return ProcessResult(
        ProcessDisposition.EXITED,
        stdout=stdout,
        stderr=stderr,
        exit_code=returncode,
    )


class _OutputLimitExceeded(Exception):
    pass


async def _collect(
    process: asyncio.subprocess.Process,
    stdout_buffer: bytearray,
    stderr_buffer: bytearray,
    *,
    max_output_bytes: int | None,
    stdin: bytes | None,
) -> None:
    async def drain(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
        while stream is not None and (chunk := await stream.read(8192)):
            if max_output_bytes is not None and len(stdout_buffer) + len(stderr_buffer) + len(chunk) > max_output_bytes:
                raise _OutputLimitExceeded
            buffer.extend(chunk)

    async def feed() -> None:
        if stdin is None or process.stdin is None:
            return
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    await asyncio.gather(
        feed(),
        drain(process.stdout, stdout_buffer),
        drain(process.stderr, stderr_buffer),
        process.wait(),
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2.0)


def _decode(value: bytearray, *, strict: bool) -> str:
    return bytes(value).decode(errors="strict" if strict else "replace").strip()


__all__ = [
    "AuthorizedShellIntent",
    "ProcessDisposition",
    "ProcessResult",
    "UserCommandSandbox",
    "run_authorized_shell",
    "run_fixed_argv",
    "run_verified_fixed_argv",
    "FixedExecutableBinding",
]
