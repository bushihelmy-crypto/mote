"""Product wiring for the bundled runtime LSP capability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mote.runtime.lsp import DiagnosticsBuffer, LspService


class ProductLspServiceFactory:
    def build_service(self, config: Any, project_root: Path) -> LspService:
        return LspService(config, project_root)

    def build_diagnostics_provider(self) -> DiagnosticsBuffer:
        return DiagnosticsBuffer()


__all__ = ["ProductLspServiceFactory"]
