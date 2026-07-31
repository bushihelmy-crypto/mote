"""LspServerManager — lazily launch + route to per-language servers.

Holds the live :class:`LspServerInstance`s for one Role session, keyed by server
name, and the shared :class:`DiagnosticRegistry` they all publish into. Servers
are launched lazily on the first edit of a file they handle (a server that fails
to start is remembered as dead so we don't retry it every edit).

One manager per session; the service owns it. No global singleton (unlike the
terminal engine) — LSP state is per-Role and torn down with the session.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from mote.runtime.config.lsp import LspConfig
from mote.runtime.lsp.registry import DiagnosticRegistry
from mote.runtime.lsp.server import LspServerInstance


class LspServerManager:
    """Owns the session's language servers + their shared diagnostic registry."""

    def __init__(self, config: LspConfig, root_path: str) -> None:
        self.config = config
        self.root_path = root_path
        self.registry = DiagnosticRegistry()
        self._servers: dict[str, LspServerInstance] = {}
        self._failed: set[str] = set()  # server names that failed to start
        self._lock = asyncio.Lock()

    async def server_for(self, path: str) -> Optional[LspServerInstance]:
        """Return a started server handling *path*, launching it lazily, or None.

        Returns None when no server is configured for the file, the subsystem is
        disabled, or the matching server previously failed to start.
        """
        server_config = self.config.server_for(path)
        if server_config is None:
            return None
        name = server_config.name
        if name in self._failed:
            return None

        existing = self._servers.get(name)
        if existing is not None and existing.alive:
            return existing

        async with self._lock:
            # Re-check under the lock (another await may have started it).
            existing = self._servers.get(name)
            if existing is not None and existing.alive:
                return existing
            if name in self._failed:
                return None
            instance = LspServerInstance(
                server_config,
                self.root_path,
                self.registry,
                init_timeout=self.config.init_timeout,
                diagnostics_wait=self.config.diagnostics_wait,
            )
            if await instance.start():
                self._servers[name] = instance
                return instance
            self._failed.add(name)
            return None

    async def document_symbols(self, path: str) -> list:
        """documentSymbol for *path* via its server (``[]`` when none/failed)."""
        server = await self.server_for(path)
        if server is None:
            return []
        return await server.document_symbols(path)

    async def definition(self, path: str, line: int, character: int) -> list:
        """definition at ``(line, character)`` in *path* (``[]`` when none/failed)."""
        server = await self.server_for(path)
        if server is None:
            return []
        return await server.definition(path, line, character)

    async def references(self, path: str, line: int, character: int) -> list:
        """references to the symbol at ``(line, character)`` (``[]`` when none/failed)."""
        server = await self.server_for(path)
        if server is None:
            return []
        return await server.references(path, line, character)

    async def shutdown(self) -> None:
        """Shut down all managed servers. Idempotent."""
        servers = list(self._servers.values())
        self._servers.clear()
        for server in servers:
            await server.shutdown()


__all__ = ["LspServerManager"]
