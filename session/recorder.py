"""SessionRecorder — the durable-log sink injected into ContextManager.

A thin adapter satisfying ``metagpt.common.interface.SessionRecorder``: it turns
the two events the context layer emits (appended message, compaction checkpoint)
into ``SessionLog`` appends. Keeping this separate from ``SessionLog`` lets the
context layer depend only on the narrow protocol while the roles layer owns the
concrete log + event construction.

``enabled`` gates recording: turned off while replaying a resumed session so the
rebuild path (which re-adds messages) does not re-record them (Phase 2). Calls
are best-effort and never raise into the caller — a logging failure must not
break the agent's turn.
"""

from __future__ import annotations

from typing import List

from metagpt.common.logs import log_class, logger
from metagpt.common.schema import Message
from metagpt.session.events import CompactedEvent, MessageEvent
from metagpt.session.log import SessionLog


@log_class(level="DEBUG", exclude={"record_message", "record_compaction"})
class SessionRecorder:
    """Streams context-layer events to a :class:`SessionLog`."""

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    def record_message(self, message: Message) -> None:
        if not self.enabled or message is None:
            return
        try:
            self._log.append(MessageEvent(message=message))
        except Exception as exc:  # noqa: BLE001 — logging must not break a turn
            logger.warning(f"SessionRecorder: failed to record message: {exc}")

    def record_compaction(self, messages: List[Message], summary: str) -> None:
        if not self.enabled:
            return
        try:
            self._log.append(CompactedEvent(messages=list(messages), summary=summary))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SessionRecorder: failed to record compaction: {exc}")


__all__ = ["SessionRecorder"]
