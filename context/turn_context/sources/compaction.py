"""CompactionNoticeContextSource — flag that history was just auto-compacted.

A *reactive* counterpart to the proactive ``TokenPressureContextSource``: the
latter warns *before* compaction ("context filling up, write things down"); this
one tells the model *after* it happened ("history was condensed"), so the model
doesn't silently notice a shorter conversation on the next turn and wonder what
was dropped.

Push→pull bridge in one object (the ``ContextManager`` is already the producer,
so unlike the LSP feed no separate buffer object is needed):
- as an :class:`~metagpt.common.interface.EventSubscriber` it catches
  :class:`~metagpt.common.events.PostCompactEvent` off the bus and arms a
  pending flag (several compactions between turns collapse into one notice);
- as an :class:`~metagpt.common.interface.EphemeralContextSource` it renders the
  notice once per think() cycle and disarms (so it shows exactly once, the turn
  after the compaction, then goes quiet).

The autocompact summary itself is *not* repeated here — it already lives in the
rebuilt history as the first message; this feed only flags that it happened.
"""

from __future__ import annotations

from typing import Optional

from metagpt.common.events import PostCompactEvent


class CompactionNoticeContextSource:
    """Renders a one-shot notice the turn after an automatic compaction."""

    name = "compaction"
    # Render order in the turn-context bus: between token-pressure (20, the
    # pre-compaction warning) and background tasks (30). The same value serves as
    # the EventSubscriber dispatch priority, where it is immaterial (this handler
    # only observes — it returns no outcome).
    priority = 25

    def __init__(self) -> None:
        self._pending = False

    async def handle(self, event) -> None:
        """Arm the notice on a PostCompactEvent; ignore everything else."""
        if isinstance(event, PostCompactEvent):
            self._pending = True
        return None

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
