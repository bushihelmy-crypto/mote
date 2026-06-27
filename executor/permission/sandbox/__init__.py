"""Filesystem sandbox — the boundary axis, orthogonal to the approval mode.

See :mod:`.guard`. No module here imports tools or the executor.
"""
from __future__ import annotations

from metagpt.executor.permission.sandbox.guard import SandboxGuard, SandboxVerdict

__all__ = ["SandboxGuard", "SandboxVerdict", "ResourceGuard", "build_policy", "build_runtime"]


def __getattr__(name: str):
    # Lazily expose the adapter helpers + ResourceGuard so importing this package
    # does not pull in the runtime (``metagpt.sandbox``) unless an OS-level
    # sandbox is wired.
    if name in ("build_policy", "build_runtime"):
        from metagpt.executor.permission.sandbox import adapter

        return getattr(adapter, name)
    if name == "ResourceGuard":
        from metagpt.executor.permission.sandbox.resource_guard import ResourceGuard

        return ResourceGuard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
