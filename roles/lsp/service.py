"""LspService — an event-spine subscriber that both consumes and produces.

The single object the Role lazily builds (gated on ``role_schema.lsp.enabled``)
and subscribes to the shared :class:`~mote.common.events.EventBus`. It wires
the manager (lazy server launch + routing) to the agent's file-mutation signal
on the **input** side and broadcasts diagnostics on the **output** side:

- ``handle(event)`` : on a :class:`FileMutatedEvent` (a tool just wrote a file),
  route the edit to a matching server, sync the doc, let diagnostics publish,
  then emit a :class:`DiagnosticsEvent` carrying any *changed* set;
- ``shutdown()`` : tear down all servers (called on session cleanup by the Role).

It is an :class:`~mote.common.interface.ObservationSubscriber` on both edges: the
executor does not poke it directly (it emits a ``FileMutatedEvent`` this
service subscribes to), and the diagnostics it produces ride the bus as a
``DiagnosticsEvent`` (the :class:`DiagnosticsBuffer` accumulates them for
next-turn context; other subscribers may react too). The emit is gated on a
wired :attr:`bus`, so a bus-less service (direct ``file_saved`` calls in tests)
stays inert. Best-effort throughout: every method swallows its own errors so the
LSP layer can never break a turn.
"""

from __future__ import annotations

from typing import List, Tuple

from mote.common.events import DiagnosticsEvent, FileMutatedEvent
from mote.common.interface.event_subscriber import BusAware, ObservationSubscriber, ObserverPriority
from mote.common.logs import logger
from mote.common.schema import LspConfig
from mote.roles.lsp.format import format_diagnostics
from mote.roles.lsp.manager import LspServerManager


class LspService(ObservationSubscriber, BusAware):
    """LSP diagnostics for one Role session; an :class:`ObservationSubscriber`."""

    # ObservationSubscriber priority: LIVE — runs before the file-watcher's late
    # bookkeeping, but its ordering vs other subscribers is immaterial (it only
    # reacts to FileMutatedEvent).
    priority: int = ObserverPriority.LIVE

    def __init__(self, config: LspConfig, root_path: str, *, bus=None) -> None:
        self._manager = LspServerManager(config, root_path)
        # The event bus to broadcast DiagnosticsEvent on. Wired by the bus itself
        # via ``on_subscribed`` when the service is subscribed (see below);
        # ``None`` (tests wiring ``file_saved`` directly / no bus) disables emit.
        self.bus = bus

    def on_subscribed(self, bus) -> None:
        """Bus lifecycle hook: capture the bus handle we broadcast diagnostics on.

        This service is a dual-role subscriber — it *consumes* FileMutatedEvent
        (input edge) and *produces* DiagnosticsEvent (output edge). The bus calls
        this once on ``subscribe`` so the producer edge gets its handle without
        the host special-casing a back-reference assignment.
        """
        self.bus = bus

    async def handle(self, event) -> None:
        """Sync a just-mutated file, then broadcast any changed diagnostics."""
        if isinstance(event, FileMutatedEvent) and event.path:
            await self.file_saved(event.path)
            await self._publish_diagnostics()
        return None

    async def file_saved(self, path: str) -> None:
        """Sync a just-written file to its language server (best-effort no-op)."""
        if not path:
            return
        try:
            server = await self._manager.server_for(path)
            if server is not None:
                await server.did_save(path)
        except Exception as exc:  # noqa: BLE001 — never break the tool/turn
            logger.debug(f"LspService: did_save for {path} failed: {exc}")

    async def _publish_diagnostics(self) -> None:
        """Drain any changed diagnostics and broadcast them on the bus."""
        if self.bus is None:
            return
        try:
            block, paths = self._drain_changed()
            if block:
                await self.bus.emit(DiagnosticsEvent(block=block, paths=paths))
        except Exception as exc:  # noqa: BLE001 — never break the turn
            logger.debug(f"LspService: diagnostics publish failed: {exc}")

    def _drain_changed(self) -> Tuple[str, List[str]]:
        """Render changed diagnostics + their paths, marking them delivered."""
        if not self._manager.registry.has_changes():
            return "", []
        changed = self._manager.registry.drain_changed()
        return format_diagnostics(changed), list(changed.keys())

    def drain_diagnostics(self) -> str:
        """Render changed diagnostics as a context block, or "" when none.

        The pull counterpart of the bus emit — kept for callers that drain the
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
        try:
            await self._manager.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LspService: manager shutdown failed: {exc}")


__all__ = ["LspService"]
