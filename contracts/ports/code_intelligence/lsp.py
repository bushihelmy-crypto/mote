"""Injected LSP runtime capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class DiagnosticsProvider(Protocol):
    name: str
    priority: int
    save_to_context: bool

    async def handle(self, event: object) -> None:
        ...

    async def render(self, *, cwd: str | None = None) -> str | None:
        ...


class LspServiceFactory(Protocol):
    def build_service(self, config: Any, project_root: Path) -> Any:
        ...

    def build_diagnostics_provider(self) -> DiagnosticsProvider:
        ...


__all__ = ["DiagnosticsProvider", "LspServiceFactory"]
