"""LspServerInstance — one language server subprocess, driven over JSON-RPC.

Owns a single launched language server and the LSP semantics on top of the raw
:class:`JsonRpcEndpoint` transport:

- ``start()``  : spawn the process, run the ``initialize`` handshake, send
  ``initialized``;
- ``did_save(path)`` : open the doc on first sight (``textDocument/didOpen``),
  otherwise ``didChange``, then ``didSave``, and wait a short window for the
  server to publish diagnostics;
- a notification handler routes ``textDocument/publishDiagnostics`` into the
  shared :class:`DiagnosticRegistry`;
- ``shutdown()`` : polite ``shutdown``+``exit``, then kill the process.

Everything is best-effort: a server that fails to launch / initialise leaves the
instance ``alive == False`` and all later calls become no-ops, so a broken server
never breaks a turn.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from mote.common.logs import logger
from mote.common.schema import LspServerConfig
from mote.common.text import path_to_uri, uri_to_path
from mote.roles.lsp.jsonrpc import JsonRpcEndpoint
from mote.roles.lsp.registry import DiagnosticRegistry, parse_diagnostic


class LspServerInstance:
    """A single managed language server."""

    def __init__(
        self,
        config: LspServerConfig,
        root_path: str,
        registry: DiagnosticRegistry,
        *,
        init_timeout: float = 10.0,
        diagnostics_wait: float = 1.5,
    ) -> None:
        self.config = config
        self.root_path = root_path
        self.registry = registry
        self.init_timeout = init_timeout
        self.diagnostics_wait = diagnostics_wait

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._endpoint: Optional[JsonRpcEndpoint] = None
        self._open_docs: dict[str, int] = {}  # uri -> document version
        self.alive = False

    @property
    def language_id(self) -> str:
        return self.config.language_id or self.config.name

    async def start(self) -> bool:
        """Launch + initialize the server. Returns True on success (idempotent)."""
        if self.alive:
            return True
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.root_path if os.path.isdir(self.root_path) else None,
            )
        except (OSError, ValueError):
            self._proc = None
            return False
        if self._proc.stdin is None or self._proc.stdout is None:
            await self._kill_proc()
            return False

        self._endpoint = JsonRpcEndpoint(self._proc.stdin, self._proc.stdout, on_notification=self._on_notification)
        self._endpoint.start()

        try:
            await self._endpoint.request("initialize", self._initialize_params(), timeout=self.init_timeout)
        except Exception as exc:  # noqa: BLE001 — handshake failed; tear down
            logger.debug(f"LspServer: initialize handshake failed, tearing down: {exc}")
            await self.shutdown()
            return False

        self._endpoint.notify("initialized", {})
        self.alive = True
        return True

    async def did_save(self, path: str) -> None:
        """Sync *path* to the server (open/change + save). Best-effort no-op when dead."""
        if not self.alive or self._endpoint is None:
            return
        uri = self._ensure_open(path)
        if uri is None:
            return
        self._endpoint.notify(
            "textDocument/didSave", {"textDocument": {"uri": uri}, "text": self._read_text(path) or ""}
        )
        # Give the server a moment to analyze + publish diagnostics. They arrive
        # asynchronously via _on_notification into the shared registry.
        await asyncio.sleep(self.diagnostics_wait)

    def _ensure_open(self, path: str) -> Optional[str]:
        """Open *path* on first sight (didOpen), else re-sync it (didChange).

        Returns the file's URI, or ``None`` when the doc can't be read / the
        server is dead. Shared with :meth:`did_save` so a *query* (Layer B
        targets an untouched file the agent never edited) can open it too.
        """
        if not self.alive or self._endpoint is None:
            return None
        text = self._read_text(path)
        if text is None:
            return None
        uri = path_to_uri(path)
        if uri not in self._open_docs:
            self._open_docs[uri] = 1
            self._endpoint.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.language_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
        else:
            version = self._open_docs[uri] + 1
            self._open_docs[uri] = version
            self._endpoint.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],  # full-document sync
                },
            )
        return uri

    async def document_symbols(self, path: str) -> list:
        """The file's top-level symbol table (``textDocument/documentSymbol``).

        Opens the doc first, then requests its symbols. Best-effort: any failure
        (dead server, timeout, malformed reply) yields ``[]`` — mirrors
        :meth:`did_save`'s guards so a query never breaks a turn.
        """
        if not self.alive or self._endpoint is None:
            return []
        uri = self._ensure_open(path)
        if uri is None:
            return []
        try:
            result = await self._endpoint.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                timeout=self.diagnostics_wait + 2.0,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspServer: documentSymbol for {path} failed: {exc}")
            return []
        return result if isinstance(result, list) else []

    async def definition(self, path: str, line: int, character: int) -> list:
        """Definition sites for the symbol at ``(line, character)`` in *path*.

        LSP ``textDocument/definition``. Opens the doc first. Best-effort: any
        failure yields ``[]``. The reply may be a single ``Location`` or a list;
        this normalizes to a list.
        """
        if not self.alive or self._endpoint is None:
            return []
        uri = self._ensure_open(path)
        if uri is None:
            return []
        try:
            result = await self._endpoint.request(
                "textDocument/definition",
                {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
                timeout=self.diagnostics_wait + 2.0,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspServer: definition for {path} failed: {exc}")
            return []
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []

    async def references(self, path: str, line: int, character: int) -> list:
        """Reference (call) sites for the symbol at ``(line, character)`` in *path*.

        LSP ``textDocument/references`` with ``includeDeclaration: false`` (the
        definition site itself is not a caller). Opens the doc first. Best-effort:
        any failure yields ``[]``. The reply is a list of ``Location``; a non-list
        reply normalizes to ``[]``.
        """
        if not self.alive or self._endpoint is None:
            return []
        uri = self._ensure_open(path)
        if uri is None:
            return []
        try:
            result = await self._endpoint.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                    "context": {"includeDeclaration": False},
                },
                timeout=self.diagnostics_wait + 2.0,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort query
            logger.debug(f"LspServer: references for {path} failed: {exc}")
            return []
        return result if isinstance(result, list) else []

    @staticmethod
    def _read_text(path: str) -> Optional[str]:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    async def shutdown(self) -> None:
        """Politely shut down the server, then kill the process. Idempotent."""
        self.alive = False
        endpoint = self._endpoint
        self._endpoint = None
        if endpoint is not None:
            try:
                await endpoint.request("shutdown", {}, timeout=2.0)
                endpoint.notify("exit", {})
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"LspServer: graceful shutdown handshake failed: {exc}")
            await endpoint.close()
        await self._kill_proc()

    # --- internals ---------------------------------------------------------

    def _on_notification(self, method: str, params: dict) -> None:
        """Handle server->client notifications (only diagnostics matter here)."""
        if method != "textDocument/publishDiagnostics":
            return
        uri = params.get("uri")
        if not uri:
            return
        raw_diags = params.get("diagnostics") or []
        parsed = [d for d in (parse_diagnostic(r) for r in raw_diags) if d is not None]
        self.registry.publish(uri_to_path(uri), parsed)

    def _initialize_params(self) -> dict:
        root_uri = path_to_uri(self.root_path) if os.path.isdir(self.root_path) else None
        return {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": self.root_path if os.path.isdir(self.root_path) else None,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": False},
                    "synchronization": {
                        "didSave": True,
                        "dynamicRegistration": False,
                    },
                    # Layer B: advertise the request capabilities the code map
                    # uses to resolve dangling-import symbols and (on-demand)
                    # name the exact call sites of an interface-changed symbol.
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                }
            },
            "workspaceFolders": ([{"uri": root_uri, "name": "workspace"}] if root_uri else None),
        }

    async def _kill_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (ProcessLookupError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass


__all__ = ["LspServerInstance", "path_to_uri"]
