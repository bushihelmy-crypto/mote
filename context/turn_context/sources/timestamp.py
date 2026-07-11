"""TimestampContextSource — surface the current local time, per turn.

The wall-clock time used to be baked into the user prompt's ``current_state`` line
(``prompt_builder._user_substitutions``), which meant every turn carried a fresh,
second-precision timestamp on the request tail. This source moves that fact into
the structured per-turn ``<system-reminder>`` envelope instead, alongside the other
ephemeral context, so the tail's volatile content is collected in one place rather
than hand-spliced into the command template.

Ephemeral (request-only): a timestamp is meaningful only on the turn it is shown;
it is never persisted into history (persisting it would move the prefix-cache fork
point earlier and leave a stale time in the record). It rides the volatile tail —
which is already request-only — so it never touches the cacheable system prompt.
"""

from __future__ import annotations

from typing import Optional

from mote.common.interface import TurnContextPriority
from mote.common.utils.role_utils import get_time_info


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
        return f"# Current time\n{get_time_info()}"


__all__ = ["TimestampContextSource"]
