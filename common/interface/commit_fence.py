"""Leaf interface for fencing irreversible run commits."""
from __future__ import annotations

from typing import ContextManager, Protocol, runtime_checkable


@runtime_checkable
class CommitFence(Protocol):
    """Serialize a commit against lease takeover and reject stale owners."""

    def assert_current(self, run_id: str, fencing_token: int) -> None:
        ...

    def guard(self, run_id: str, fencing_token: int) -> ContextManager[None]:
        ...


__all__ = ["CommitFence"]
