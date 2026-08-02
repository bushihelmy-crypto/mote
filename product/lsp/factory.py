"""Product wiring for the bundled runtime LSP capability."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.events.telemetry import DiagnosticsEvent
from mote.contracts.ports.events.telemetry import TelemetryEmitter
from mote.runtime.config.lsp import LspConfig
from mote.runtime.lsp import DiagnosticsBuffer, LspService


class ProductLspServiceFactory:
    def build_service(
        self,
        config: object,
        project_root: Path,
        telemetry: TelemetryEmitter[DiagnosticsEvent],
    ) -> LspService:
        if not isinstance(config, LspConfig):
            raise TypeError("Product LSP factory requires the canonical LspConfig")
        return LspService(config, str(project_root), telemetry=telemetry)

    def build_diagnostics_provider(self) -> DiagnosticsBuffer:
        return DiagnosticsBuffer()


__all__ = ["ProductLspServiceFactory"]
