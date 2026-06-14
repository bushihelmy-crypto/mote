"""Filesystem sandbox — the boundary axis, orthogonal to the approval mode.

See :mod:`.guard`. No module here imports tools or the executor.
"""
from __future__ import annotations

from metagpt.executor.permission.sandbox.guard import SandboxGuard, SandboxVerdict

__all__ = ["SandboxGuard", "SandboxVerdict"]
