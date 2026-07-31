"""Product wiring for the bundled runtime LSP capability."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.ports.events.telemetry import TelemetryEmitter
from mote.runtime.config.lsp import LspConfig
from mote.runtime.lsp import DiagnosticsBuffer, LspService


class ProductLspServiceFactory:
    def build_service(self, config: LspConfig, project_root: Path, telemetry: TelemetryEmitter) -> LspService:
        return LspService(config, project_root, telemetry=telemetry)

    def build_diagnostics_provider(self) -> DiagnosticsBuffer:
        return DiagnosticsBuffer()


__all__ = ["ProductLspServiceFactory"]
