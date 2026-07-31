"""TimestampContextSource — surface the current local time, per turn.

The wall-clock time is surfaced through the structured per-turn
``<system-reminder>`` envelope, alongside the other ephemeral context, rather than
baked into the user prompt's ``current_state`` line (which would put a fresh,
second-precision timestamp on the request tail every turn). This keeps the tail's
volatile content collected in one place rather than hand-spliced into the command
template.

Ephemeral (request-only): a timestamp is meaningful only on the turn it is shown;
it is never persisted into history (persisting it would move the prefix-cache fork
point earlier and leave a stale time in the record). It rides the volatile tail —
which is already request-only — so it never touches the cacheable system prompt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from mote.contracts.ports.conversation.turn_context import TurnContextPriority


def _local_time_text() -> str:
    current_time = datetime.now().astimezone()
    return f"Current local time is {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}."


class TimestampContextSource:
    """Emits the current local time each turn as an ephemeral reminder block."""

    name = "timestamp"
    # Late in the envelope: the wall-clock time is ambient orientation, not an
    # urgent freshness/pressure signal — it trails the curated feeds.
    priority = TurnContextPriority.SKILL_LISTING
    # Ephemeral (request-only): volatile-tail content, never persisted, never the
    # cacheable system prompt.
    save_to_context = False

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        return _local_time_text()


__all__ = ["TimestampContextSource"]
