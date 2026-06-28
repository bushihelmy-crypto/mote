"""RecorderSubscriber — the durable-log sink, now an event-bus subscriber.

Replaces ``session/recorder.py``'s ``SessionRecorder``: instead of being a sink
injected into ``ContextManager``, it subscribes to the unified event bus and
maps the agent's lifecycle events (message / compaction / turn-end) to
``session/events.py`` records appended to a :class:`SessionLog`. Screen (renderer
subscriber) and disk (this) are now fed by the *same* event stream, so they can
no longer diverge.

The ``session_meta`` first line is **not** written here: the
:attr:`~metagpt.roles.role_components.RoleComponents.session_log` property writes
it when it builds the log (before this subscriber is even constructed), so meta
has a single source of truth and this sink only appends.

It runs at a **high priority** so it persists after the hook subscriber has had
its say (a vetoed action is never recorded as having happened).

``enabled`` gates recording (turned off while replaying a resumed session).

This is the **durable** sink (``delivery = DURABLE``): unlike a mirror observer,
its failures are *not* swallowed here. The bus's durable branch surfaces them —
logged loud and counted in :attr:`~metagpt.common.events.bus.EventBus.durable_failures`
— because a dropped rollout record is real data loss, not a cosmetic mirror miss.
The bus also never times this sink out: it must complete.
"""

from __future__ import annotations

from metagpt.common.events.types import (
    CompactionCheckpointEvent,
    LLMResponseEvent,
    MessageAppendedEvent,
    TurnEndEvent,
)
from metagpt.common.interface.event_subscriber import DURABLE, DeliveryPolicy
from metagpt.common.logs import log_class
from metagpt.session.events import (
    CompactedEvent,
    LLMCallEvent,
    MessageEvent,
    TurnContextEvent,
)
from metagpt.session.log import SessionLog
from metagpt.common.disk import get_disk_writer


@log_class(level="DEBUG", exclude={"handle"})
class RecorderSubscriber:
    """Streams bus events to a :class:`SessionLog` (the session rollout)."""

    #: Run after the hook subscriber so vetoes are folded before we persist.
    priority: int = 80
    #: Opt into durable delivery: never time-boxed, failures surfaced not dropped.
    delivery: DeliveryPolicy = DURABLE

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    async def handle(self, event) -> None:
        if not self.enabled:
            return
        if isinstance(event, MessageAppendedEvent):
            if event.message is not None:
                self._log.append(MessageEvent(message=event.message))
        elif isinstance(event, LLMResponseEvent):
            # Compact per-request telemetry: token usage + cost only (the
            # prompt/completion already land as message records). Skip the
            # no-usage placeholder calls so the rollout isn't polluted.
            if event.usage is not None:
                self._log.append(
                    LLMCallEvent(
                        request_id=event.request_id,
                        model=event.model,
                        usage=event.usage,
                        cost_usd=event.cost_usd,
                        latency_ms=event.latency_ms,
                    )
                )
        elif isinstance(event, CompactionCheckpointEvent):
            self._log.append(
                CompactedEvent(messages=list(event.messages), summary=event.summary or "")
            )
        elif isinstance(event, TurnEndEvent):
            self._log.append(
                TurnContextEvent(
                    turn_id=event.turn_id,
                    working_dir=event.working_dir,
                    model=event.model,
                    token_state=event.token_state,
                )
            )
            # Durability checkpoint: flush this turn's queued writes to disk
            # so the rollout is complete at the turn boundary (a crash before
            # the next turn loses only an in-progress, unfinished turn).

            await get_disk_writer().drain()


__all__ = ["RecorderSubscriber"]
