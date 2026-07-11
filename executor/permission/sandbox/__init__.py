"""Filesystem sandbox — the boundary axis, orthogonal to the approval mode.

See :mod:`.guard`. No module here imports tools or the executor.
"""
from __future__ import annotations

from typing import Any

from mote.executor.permission.sandbox.guard import SandboxGuard, SandboxVerdict

__all__ = ["SandboxGuard", "SandboxVerdict", "ResourceGuard", "build_policy", "build_runtime"]


def __getattr__(name: str) -> Any:
    # Lazily expose the adapter helpers + ResourceGuard so importing this package
    # does not pull in the runtime (``mote.sandbox``) unless an OS-level
    # sandbox is wired.
    if name in ("build_policy", "build_runtime"):
        from mote.executor.permission.sandbox import adapter

        return getattr(adapter, name)
    if name == "ResourceGuard":
        from mote.executor.permission.sandbox.resource_guard import ResourceGuard

        return ResourceGuard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
