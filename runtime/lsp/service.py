"""LSP projection driven by confirmed file versions; diagnostics are advisory."""

from __future__ import annotations

from collections.abc import Mapping

from mote.contracts.events.envelope import EventEnvelope, JsonValue
from mote.contracts.events.file.facts import FileTransactionCommittedEvent
from mote.contracts.events.telemetry import DiagnosticsEvent
from mote.runtime.config.lsp import LspConfig
from mote.runtime.lsp.format import format_diagnostics
from mote.runtime.lsp.manager import LspServerManager
from mote.runtime.telemetry.logging import logger


class LspService:
    """LSP diagnostics for one Role session."""

    def __init__(self, config: LspConfig, root_path: str, *, telemetry=None) -> None:
        self._manager = LspServerManager(config, root_path)
        self._telemetry = telemetry

    async def handle(self, envelope: EventEnvelope[Mapping[str, JsonValue]]) -> None:
        """Synchronize exact versions from one committed File Operations fact."""
        event = FileTransactionCommittedEvent.from_payload(dict(envelope.payload))
        for path in event.paths:
            await self.file_saved(path)
        await self._publish_diagnostics()

    async def file_saved(self, path: str) -> None:
        """Sync a confirmed file version, raising so reliable delivery can retry."""
        if not path:
            return
        server = await self._manager.server_for_confirmed_transition(path)
        if server is not None:
            await server.did_save(path)

    async def _publish_diagnostics(self) -> None:
        """Drain changed diagnostics and publish them to Telemetry."""
        if self._telemetry is None:
            return
        try:
            block, paths = self._drain_changed()
            if block:
                await self._telemetry.emit(DiagnosticsEvent(block=block, paths=paths))
        except Exception as exc:  # noqa: BLE001 — never break the turn
            logger.debug(f"LspService: diagnostics publish failed: {exc}")

    def _drain_changed(self) -> tuple[str, list[str]]:
        """Render changed diagnostics + their paths, marking them delivered."""
        if not self._manager.registry.has_changes():
            return "", []
        changed = self._manager.registry.drain_changed()
        return format_diagnostics(changed), list(changed.keys())

    def drain_diagnostics(self) -> str:
        """Render changed diagnostics as a context block, or "" when none.

        The pull counterpart of telemetry emission — kept for callers that drain the
        service directly (it marks drained diagnostics delivered so unchanged
        sets aren't re-shown). In the wired Role, diagnostics flow push-style via
        :class:`DiagnosticsEvent` instead, drained from the buffer.
        """
        try:
            return self._drain_changed()[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LspService: drain_diagnostics failed: {exc}")
            return ""

    async def document_symbols(self, path: str) -> list:
        """The file's symbol table via LSP (Layer B; ``[]`` on any failure)."""
        try:
            return await self._manager.document_symbols(path)
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspService: document_symbols for {path} failed: {exc}")
            return []

    async def definition(self, path: str, line: int, character: int) -> list:
        """Definition sites for the symbol at a position (``[]`` on any failure)."""
        try:
            return await self._manager.definition(path, line, character)
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspService: definition for {path} failed: {exc}")
            return []

    async def references(self, path: str, line: int, character: int) -> list:
        """Reference (call) sites for the symbol at a position (``[]`` on any failure)."""
        try:
            return await self._manager.references(path, line, character)
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspService: references for {path} failed: {exc}")
            return []

    async def shutdown(self) -> None:
        """Tear down all managed language servers."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close every language server owned by this subscriber."""

        await self._manager.shutdown()


__all__ = ["LspService"]
