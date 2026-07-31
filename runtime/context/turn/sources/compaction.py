"""CompactionNoticeContextSource — flag that history was just auto-compacted.

A *reactive* counterpart to the proactive ``TokenPressureContextSource``: the
latter warns *before* compaction ("context filling up, write things down"); this
one tells the model *after* it happened ("history was condensed"), so the model
doesn't silently notice a shorter conversation on the next turn and wonder what
was dropped.

After the compaction fact commits, ``ContextManager`` invokes the source's
explicit ``on_model_context_rebuilt`` projection callback before replacing the
live model context. A rebuild carrying an autocompact summary arms a pending
flag; a tool-result fold has no summary and stays silent. The ephemeral-context
surface renders the notice once on the next think cycle and disarms. Telemetry
is observation-only and is not part of this transition.

The autocompact summary itself is *not* repeated here — it already lives in the
rebuilt history as the first message; this feed only flags that it happened.
"""

from __future__ import annotations

from typing import Optional

from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.runtime.events import PostCompactEvent


class CompactionNoticeContextSource:
    """Renders a one-shot notice the turn after an automatic compaction."""

    name = "compaction"
    # Render order in the turn-context bus: between token-pressure (the
    # pre-compaction warning) and background tasks.
    priority = TurnContextPriority.COMPACTION
    # Ephemeral (request-only): a one-shot "history was just compacted" flag,
    # meaningful only on the turn after the event. It is self-disarming, so
    # persisting it would leave a permanent stale notice in history.
    save_to_context = False

    def __init__(self) -> None:
        self._pending = False

    async def on_model_context_rebuilt(self, event: object) -> None:
        """Arm the notice after a summary rebuild; ignore cheaper folds."""
        if isinstance(event, PostCompactEvent) and event.summary:
            self._pending = True

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        if not self._pending:
            return None
        self._pending = False
        return (
            "# History compacted\n"
            "Your conversation history was automatically compacted to free up "
            "context. Earlier turns have been condensed into a summary (now at "
            "the top of the conversation). If you need specifics that may have "
            "been dropped, re-read the relevant files rather than relying on "
            "memory of the older messages."
        )


__all__ = ["CompactionNoticeContextSource"]
