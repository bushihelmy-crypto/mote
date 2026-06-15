"""LspContextSource — surface fresh LSP diagnostics as an ephemeral feed.

Migrated out of ``Role.run`` (where diagnostics were prepended to the stored
user message) into the unified per-turn layer. Now diagnostics:
- are drained per think() cycle (so errors introduced mid-turn surface on the
  very next cycle, not only when a new user message arrives);
- ride in the cycle's ``<system-reminder>`` and are NOT stored in history (truly
  ephemeral, like the other feeds).

Drain-once semantics (the registry marks delivered sets) prevent re-showing
unchanged diagnostics. Duck-typed: holds any object exposing a synchronous
``drain_diagnostics() -> str`` (the ``LspService``).
"""

from __future__ import annotations

from typing import Optional


class LspContextSource:
    """Renders changed LSP diagnostics drained from the LSP service."""

    name = "lsp"
    priority = 40

    def __init__(self, service) -> None:
        # `service` is anything with a synchronous `drain_diagnostics() -> str`.
        self._service = service

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        service = self._service
        if service is None:
            return None
        block = service.drain_diagnostics()
        return block or None


__all__ = ["LspContextSource"]
