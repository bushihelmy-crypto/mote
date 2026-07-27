"""Managed lifecycle for long-lived external interactive applications."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ExternalProcessSpec:
    """Exact, shell-free launch specification for an interactive application."""

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    inherit_env: bool = True


@dataclass(frozen=True, slots=True)
class ExternalProcessHealth:
    running: bool
    pid: int | None
    return_code: int | None


class ManagedExternalProcess:
    """Own one subprocess with idempotent graceful-to-forced teardown."""

    def __init__(self, spec: ExternalProcessSpec) -> None:
        if not spec.argv:
            raise ValueError("external process argv cannot be empty")
        self.spec = spec
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> ExternalProcessHealth:
        if self._process is not None and self._process.returncode is None:
            raise RuntimeError("external process is already running")
        env = os.environ.copy() if self.spec.inherit_env else {}
        if self.spec.env:
            env.update(self.spec.env)
        self._process = await asyncio.create_subprocess_exec(
            *self.spec.argv,
            cwd=str(self.spec.cwd) if self.spec.cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.health()

    def health(self) -> ExternalProcessHealth:
        process = self._process
        return ExternalProcessHealth(
            running=process is not None and process.returncode is None,
            pid=process.pid if process is not None else None,
            return_code=process.returncode if process is not None else None,
        )

    async def wait(self) -> int:
        if self._process is None:
            raise RuntimeError("external process has not been started")
        return await self._process.wait()

    async def aclose(self, *, grace_seconds: float = 3.0) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            await process.wait()
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


__all__ = ["ExternalProcessHealth", "ExternalProcessSpec", "ManagedExternalProcess"]
