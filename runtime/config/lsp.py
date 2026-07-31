"""Runtime LSP configuration.

Lives in ``common/schema`` alongside ``hook_config.py`` / ``permission_config.py``
so ``RoleSchema`` (which declares it) can reference it without importing the LSP
service. The service itself lives in ``mote.runtime.lsp``; this is only the
declarative shape: which language servers to launch, keyed by the file
extensions they handle.

Default: a Role with ``lsp=None`` (the default) runs with no LSP layer. ``LspConfig.enabled`` gives an explicit master switch even when servers
are declared, so the whole subsystem can be toggled without dropping config.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LspServerConfig(BaseModel):
    """One language server: how to launch it and which files it handles.

    The ``command`` is spawned once per Role session and driven over stdio with
    Content-Length-framed JSON-RPC (LSP). ``extensions`` (e.g. ``[".py"]``)
    decides which edited files are routed to this server for diagnostics.
    """

    name: str = Field(description="Human-readable id for the server (e.g. 'pyright').")
    command: list[str] = Field(
        description="argv to launch the server, talking LSP over stdio (e.g. ['pyright-langserver', '--stdio'])."
    )
    extensions: list[str] = Field(
        default_factory=list,
        description="File extensions this server handles, incl. the dot (e.g. ['.py']).",
    )
    language_id: str = Field(
        default="",
        description="LSP languageId for didOpen (e.g. 'python'); defaults to the server name.",
    )

    def handles(self, path: str) -> bool:
        """True if *path*'s extension is one this server is configured for."""
        lowered = path.lower()
        return any(lowered.endswith(ext.lower()) for ext in self.extensions)


class LspConfig(BaseModel):
    """Per-Role LSP policy, declared on :class:`RoleSchema`.

    ``servers`` lists the language servers to launch lazily on first relevant
    file edit. ``enabled`` is the master switch (set False to keep the config but
    turn the subsystem off). When no server matches an edited file, nothing
    happens — the subsystem stays inert.
    """

    enabled: bool = Field(default=True, description="Master switch for the LSP subsystem.")
    servers: list[LspServerConfig] = Field(
        default_factory=list,
        description="Language servers to manage (launched lazily, one per session).",
    )
    init_timeout: float = Field(
        default=10.0,
        description="Seconds to wait for a server's initialize handshake before giving up.",
    )
    diagnostics_wait: float = Field(
        default=1.5,
        description="Seconds to wait after a save for the server to publish diagnostics.",
    )

    def server_for(self, path: str) -> Optional[LspServerConfig]:
        """Return the first configured server that handles *path*, or None."""
        if not self.enabled:
            return None
        for server in self.servers:
            if server.handles(path):
                return server
        return None


__all__ = ["LspConfig", "LspServerConfig"]
