"""LspNotifier protocol — the narrow face the ToolExecutor pokes after edits.

The structural slice the ``ToolExecutor`` depends on to tell the LSP subsystem
that a file changed on disk (so it can sync the document and collect fresh
diagnostics), without naming the concrete ``LspService`` in ``roles/lsp``.

Mirrors ``SessionRecorder`` / ``HookRunner``: ``executor`` must never import the
``roles`` layer (strict downward-only layering), so it takes this Protocol and
the concrete service is injected by ``Role``. Calls must be best-effort and
non-throwing from the executor's point of view.

Leaf module: imports only ``typing``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LspNotifier(Protocol):
    """The face the executor uses to report file mutations to the LSP layer."""

    async def file_saved(self, path: str) -> None:
        """Note that *path* was just written/edited on disk.

        The implementation routes it to a matching language server (launching
        one lazily if needed), syncs the document, and stages any diagnostics
        for delivery at the next turn boundary. No-op when no server handles the
        file. Must never raise into the caller.
        """
        ...

    async def shutdown(self) -> None:
        """Tear down all managed language servers (called on session cleanup)."""
        ...


__all__ = ["LspNotifier"]
