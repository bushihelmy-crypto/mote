"""Session replay — reconstruct stored history from a rollout log (Phase 2).

The append-only rollout is the truth source; resume rebuilds the in-memory
history from it. A single forward pass suffices because the log is ordered and a
``compacted`` event already carries the *full* post-compaction history
(``replacement_history``):

  * ``message``    -> append the message to the running history
  * ``compacted``  -> RESET the running history to ``replacement_history``
                      (later ``message`` events then append after it)
  * ``session_meta`` -> capture the session identity/cwd
  * ``turn_context`` / ``meta_update`` -> ignored for history rebuild

So the final history equals ``latest_checkpoint + messages_after_it`` — the same
state that was live when the session stopped. Pre-checkpoint message appends are
harmlessly discarded by the reset, mirroring how the live ContextManager swapped
its list on compaction. This avoids the reverse-scan Codex needs by relying on
the checkpoint being self-contained.

Reconstruction is forgiving: a message payload that fails to load is skipped
(``Message.load`` returns None) so one bad line never aborts the whole replay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from metagpt.common.logs import log_call
from metagpt.common.schema import Message
from metagpt.session.events import (
    CompactedEvent,
    MessageEvent,
    SessionMetaEvent,
    parse_event,
)
from metagpt.session.log import SessionLog


@dataclass
class ReplayResult:
    """The outcome of replaying a rollout log into a history."""

    messages: List[Message] = field(default_factory=list)
    meta: Optional[Dict] = None
    message_events: int = 0
    checkpoints: int = 0
    skipped: int = 0

    @property
    def from_checkpoint(self) -> bool:
        return self.checkpoints > 0


@log_call(level="DEBUG")
def replay(log: SessionLog) -> ReplayResult:
    """Reconstruct the stored history from ``log`` (single forward pass)."""
    result = ReplayResult()
    for record in log.iter_raw():
        event = parse_event(record)
        if isinstance(event, SessionMetaEvent):
            result.meta = asdict(event)
        elif isinstance(event, MessageEvent):
            result.message_events += 1
            if event.message is None:
                result.skipped += 1  # unloadable payload, skipped (counted)
            else:
                result.messages.append(event.message)
        elif isinstance(event, CompactedEvent):
            result.checkpoints += 1
            # Reset to the self-contained checkpoint history.
            result.messages = list(event.messages)
        # turn_context / meta_update / unknown: not part of the history rebuild.
    return result


__all__ = ["ReplayResult", "replay"]
