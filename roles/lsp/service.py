"""LspService — the LspNotifier the executor pokes, with next-turn delivery.

The single object the Role lazily builds (gated on ``role_schema.lsp.enabled``)
and injects into the ToolExecutor as the :class:`LspNotifier`. It wires the
manager (lazy server launch + routing) to the executor's after-edit seam and to
the Role's turn boundary:

- ``file_saved(path)``  : route the edit to a matching server, sync the doc,
  let diagnostics publish (called by the executor right after a file mutation);
- ``drain_diagnostics()`` : called by the Role at the start of a turn to pull
  any *changed* diagnostics and render them as a context block to inject;
- ``shutdown()`` : tear down all servers (called on session cleanup).

Best-effort throughout: every public method swallows its own errors so the LSP
layer can never break a turn.
"""

from __future__ import annotations

from metagpt.common.schema import LspConfig
from metagpt.roles.lsp.format import format_diagnostics
from metagpt.roles.lsp.manager import LspServerManager


class LspService:
    """Concrete :class:`LspNotifier` for one Role session."""

    def __init__(self, config: LspConfig, root_path: str) -> None:
        self._manager = LspServerManager(config, root_path)

    async def file_saved(self, path: str) -> None:
        """Sync a just-written file to its language server (best-effort no-op)."""
        if not path:
            return
        try:
            server = await self._manager.server_for(path)
            if server is not None:
                await server.did_save(path)
        except Exception:  # noqa: BLE001 — never break the tool/turn
            pass

    def drain_diagnostics(self) -> str:
        """Render changed diagnostics as a context block, or "" when none.

        Called at the turn boundary; marks the drained diagnostics delivered so
        unchanged sets aren't re-shown next turn.
        """
        try:
            if not self._manager.registry.has_changes():
                return ""
            changed = self._manager.registry.drain_changed()
            return format_diagnostics(changed)
        except Exception:  # noqa: BLE001
            return ""

    async def shutdown(self) -> None:
        """Tear down all managed language servers."""
        try:
            await self._manager.shutdown()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["LspService"]
