"""Violation parsing — turn a sandboxed command's stderr into structure.

When a bwrap-confined command tries to write outside its writable roots, the
kernel returns ``EPERM``/``EACCES`` and bwrap itself may print a diagnostic.
This module recognises the common shapes and lifts them into a
:class:`SandboxViolation` so the executor can surface a clear, structured
message instead of a raw errno splatter.

Best-effort and heuristic: stderr is not a stable contract, so we match a few
well-known phrases and otherwise return nothing (no false positives).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# bwrap setup failures (mount/namespace) — distinct from a sandboxed command's
# own permission errors. These mean the sandbox could not be constructed.
_BWRAP_SETUP_RE = re.compile(r"^bwrap:\s*(.+)$", re.MULTILINE)

# A confined write hitting the read-only root / outside a bind mount surfaces as
# a classic errno phrase from the failing tool (cp, touch, redirection...).
_EPERM_RE = re.compile(
    r"(Read-only file system|Permission denied|Operation not permitted)",
)


@dataclass
class SandboxViolation:
    """A structured account of a sandbox boundary breach attempt."""

    kind: str  # "fs" (filesystem write blocked) | "setup" (sandbox build failed)
    message: str
    detail: str = ""

    def render(self) -> str:
        """A one-line, human-readable summary for tool output."""
        base = f"[sandbox:{self.kind}] {self.message}"
        return f"{base} ({self.detail})" if self.detail else base


def parse_violations(stderr: str) -> list[SandboxViolation]:
    """Extract sandbox violations from a command's *stderr*.

    Returns an empty list when nothing recognisable is present (the common
    case — most commands don't trip the sandbox).
    """
    if not stderr:
        return []

    violations: list[SandboxViolation] = []

    for m in _BWRAP_SETUP_RE.finditer(stderr):
        violations.append(SandboxViolation(kind="setup", message="sandbox setup failed", detail=m.group(1).strip()))

    if not violations:
        m = _EPERM_RE.search(stderr)
        if m is not None:
            violations.append(
                SandboxViolation(
                    kind="fs",
                    message="a write was blocked by the sandbox filesystem boundary",
                    detail=m.group(1),
                )
            )

    return violations


__all__ = ["SandboxViolation", "parse_violations"]
