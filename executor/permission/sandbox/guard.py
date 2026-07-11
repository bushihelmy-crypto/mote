"""SandboxGuard — logical filesystem boundary enforcement.

Given a :class:`SandboxConfig` and a way to read the Role's cwd, decides whether
a write to a path is inside the allowed boundary. This is a *path-checking*
sandbox (not OS-level): the engine consults it before a file-mutating tool runs.

Modes:
  * ``full``            — no boundary, every write allowed.
  * ``read-only``       — no writes allowed at all.
  * ``workspace-write`` — writes confined to the cwd + configured writable roots
                          (+ any roots the user granted this session).

A violation is not a hard failure: the engine escalates it to the user, and a
"session" grant calls :meth:`add_session_root` so subsequent writes under that
directory pass without re-prompting (Codex's escalation-with-persistence flow).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from mote.common.schema import SandboxConfig


@dataclass
class SandboxVerdict:
    """Outcome of a sandbox boundary check."""

    allowed: bool
    reason: str = ""
    path: str = ""


class SandboxGuard:
    """Enforce a :class:`SandboxConfig`'s filesystem boundary."""

    def __init__(self, config: SandboxConfig, get_cwd: Optional[Callable[[], str]] = None) -> None:
        self._config = config
        self._get_cwd = get_cwd
        # Roots the user granted interactively this session (escalation "always").
        self._session_roots: list[str] = []

    @staticmethod
    def _norm(path: str) -> str:
        """Absolute, user-expanded, symlink-resolved path (for stable containment)."""
        return os.path.realpath(os.path.expanduser(path))

    def add_session_root(self, path: str) -> None:
        """Grant write access under ``path`` for the rest of the session."""
        if path:
            self._session_roots.append(self._norm(path))

    def writable_roots(self) -> list[str]:
        """The resolved set of roots that may be written under, in this mode."""
        raw: list[str] = []
        if self._get_cwd is not None:
            cwd = self._get_cwd()
            if cwd:
                raw.append(cwd)
        raw.extend(self._config.writable_roots)
        roots = [self._norm(r) for r in raw if r]
        roots.extend(self._session_roots)
        return roots

    def check_write(self, path: str) -> SandboxVerdict:
        """Decide whether writing ``path`` is within the sandbox boundary."""
        mode = self._config.mode
        if mode == "full":
            return SandboxVerdict(True)
        if not path:
            # No concrete path to gate (e.g. a tool that mutates without a path
            # target). Phase 2 only enforces path-based writes — allow.
            return SandboxVerdict(True)

        if mode == "read-only":
            return SandboxVerdict(
                False,
                reason=f"writing '{path}' is blocked in a read-only sandbox",
                path=path,
            )

        # workspace-write: must sit inside a writable root.
        resolved = self._norm(path)
        for root in self.writable_roots():
            if resolved == root or resolved.startswith(root + os.sep):
                return SandboxVerdict(True)
        return SandboxVerdict(
            False,
            reason=f"'{path}' is outside the workspace-writable roots",
            path=path,
        )
