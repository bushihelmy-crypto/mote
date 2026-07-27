"""DiagnosticsBuffer — the push→pull bridge that surfaces LSP diagnostics.

The output-side counterpart to :class:`LspService`. The service *produces*
:class:`~mote.runtime.events.DiagnosticsEvent`\\s on Telemetry when a synced edit
yields a changed diagnostic set; this object *accumulates* their rendered blocks
until the turn boundary, then *renders* them into the cycle's
``<system-reminder>``.

A single object playing both sides of the bridge (the same dual-role shape as
``CompactionNoticeContextSource``, since the producer — ``LspService`` — already
lives elsewhere, so no extra wrapper is warranted):
- as a telemetry handler it stages each
  DiagnosticsEvent and stages its block (several edits within one turn collapse
  into one drained block);
- as an :class:`~mote.contracts.ports.EphemeralContextSource` it ``render``\\s
  the accumulated blocks once per think() cycle and clears (so unchanged
  diagnostics aren't re-shown).

Bridging push→pull: edits arrive event-driven (possibly several within one
turn), but context injection pulls once per think() cycle. The buffer holds the
blocks in between and clears on drain.
"""

from __future__ import annotations

from typing import List, Optional

from mote.contracts.ports import TurnContextPriority
from mote.runtime.events import DiagnosticsEvent


class DiagnosticsBuffer:
    """Stages DiagnosticsEvent blocks and renders them as next-turn context."""

    name = "lsp"
    telemetry_observer = True
    # Render order in the turn-context bus (after git/token/compaction/tasks).
    # This object is dual-role (telemetry handler + EphemeralContextSource)
    # sharing one ``priority`` field: the turn-context render order is its
    # authoritative meaning, so it keys on TurnContextPriority (not
    # Turn-context ordering does not affect independent telemetry mailboxes.
    priority: int = TurnContextPriority.DIAGNOSTICS
    # Ephemeral (request-only): diagnostics are a one-shot drain — ``render``
    # empties ``_blocks`` so unchanged sets are never re-shown. They report "your
    # last edit produced these errors", actionable only on the turn they surface
    # (an error is often fixed the very next turn), so persisting them would just
    # leave stale diagnostics in history. Matches the drain-once contract above.
    save_to_context: bool = False

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
