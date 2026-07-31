"""Injected LSP runtime capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mote.contracts.ports.events.subscription import CommittedEventHandler
from mote.contracts.ports.events.telemetry import TelemetryEmitter


class DiagnosticsProvider(Protocol):
    name: str
    priority: int
    save_to_context: bool

    async def handle(self, event: object) -> None: ...

    async def render(self, *, cwd: str | None = None) -> str | None: ...


class LspServiceFactory(Protocol):
    """Product adapter for the Runtime-owned concrete LSP configuration."""

    def build_service(
        self,
        config: object,
        project_root: Path,
        telemetry: TelemetryEmitter,
    ) -> CommittedEventHandler: ...

    def build_diagnostics_provider(self) -> DiagnosticsProvider: ...


__all__ = ["DiagnosticsProvider", "LspServiceFactory"]
