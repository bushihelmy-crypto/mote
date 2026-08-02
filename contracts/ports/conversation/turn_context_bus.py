"""Minimal turn-context collection capability consumed by Kernel execution."""

from __future__ import annotations

from typing import Protocol


class TurnContextCollector(Protocol):
    async def collect_to_context(self, *, cwd: str | None = None) -> str: ...


__all__ = ["TurnContextCollector"]
