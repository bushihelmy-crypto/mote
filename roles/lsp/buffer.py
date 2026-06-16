"""DiagnosticsBuffer — the push→pull bridge that surfaces LSP diagnostics.

The output-side counterpart to :class:`LspService`. The service *produces*
:class:`~metagpt.common.events.DiagnosticsEvent`\\s on the bus when a synced edit
yields a changed diagnostic set; this object *accumulates* their rendered blocks
until the turn boundary, then *renders* them into the cycle's
``<system-reminder>``.

A single object playing both sides of the bridge (the same dual-role shape as
``CompactionNoticeContextSource``, since the producer — ``LspService`` — already
lives elsewhere, so no extra wrapper is warranted):
- as an :class:`~metagpt.common.interface.EventSubscriber` it ``handle``\\s each
  DiagnosticsEvent and stages its block (several edits within one turn collapse
  into one drained block);
- as an :class:`~metagpt.common.interface.EphemeralContextSource` it ``render``\\s
  the accumulated blocks once per think() cycle and clears (so unchanged
  diagnostics aren't re-shown).

Bridging push→pull: edits arrive event-driven (possibly several within one
turn), but context injection pulls once per think() cycle. The buffer holds the
blocks in between and clears on drain.
"""

from __future__ import annotations

from typing import List, Optional

from metagpt.common.events import DiagnosticsEvent


class DiagnosticsBuffer:
    """Stages DiagnosticsEvent blocks and renders them as next-turn context."""

    name = "lsp"
    # Render order in the turn-context bus (after git/token/compaction/tasks).
    # The same value serves as the EventSubscriber dispatch priority, where it is
    # immaterial — this handler only accumulates, returning no outcome.
    priority: int = 40

    def __init__(self) -> None:
        self._blocks: List[str] = []

    async def handle(self, event) -> None:
        """Stage a DiagnosticsEvent's rendered block; ignore everything else."""
        if isinstance(event, DiagnosticsEvent) and event.block:
            self._blocks.append(event.block)
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        """Turn-context source face: drain the staged blocks, or ``None``."""
        return self.drain_diagnostics() or None

    def drain_diagnostics(self) -> str:
        """Return the accumulated blocks (joined) and clear, or "" when empty."""
        if not self._blocks:
            return ""
        block = "\n\n".join(self._blocks)
        self._blocks.clear()
        return block


__all__ = ["DiagnosticsBuffer"]
