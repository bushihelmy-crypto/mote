"""EventBus — the single ordered async stream every producer/consumer shares.

The loop (and the layers it drives) is the sole **producer**: it ``emit``s
events. The recorder, hook manager, renderer, report-queue are all
**subscribers** awaited in priority order per event. This is the whole spine:
one dispatch method, sequential awaits, one bad subscriber never breaks the
stream.

Priority ordering (ascending = earlier) lets a veto land first: hooks subscribe
low (run before the recorder), so a denied tool call is folded before anything
persists it.

A sync sibling :meth:`emit_sync` exists for fire-and-forget *observation* events
raised from synchronous call sites (e.g. a tool's ``record_file_snapshot``
capability): it delivers only to subscribers exposing a sync ``handle_sync`` and
never returns an outcome (observation events have none to read).

Leaf module: imports only ``common.events`` siblings + ``common.logs``. It never
imports roles/context/executor — those inject themselves as subscribers.
"""

from __future__ import annotations

from typing import List

from metagpt.common.events.outcome import EMPTY, HookOutcome, fold
from metagpt.common.interface.event_subscriber import EventSubscriber
from metagpt.common.logs import logger


class EventBus:
    """An ordered list of subscribers fanned out to, per emitted event."""

    def __init__(self) -> None:
        self._subs: List[EventSubscriber] = []

    def subscribe(self, sub: EventSubscriber) -> None:
        """Insert ``sub`` keeping the list sorted by ascending ``priority``.

        Stable for equal priorities (insertion order preserved among ties).
        """
        priority = getattr(sub, "priority", 0)
        idx = len(self._subs)
        for i, existing in enumerate(self._subs):
            if getattr(existing, "priority", 0) > priority:
                idx = i
                break
        self._subs.insert(idx, sub)

    def unsubscribe(self, sub: EventSubscriber) -> None:
        """Remove ``sub`` if present (safe no-op when it was never subscribed)."""
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    @property
    def subscribers(self) -> List[EventSubscriber]:
        """The current subscribers in dispatch order (read-only view)."""
        return list(self._subs)

    async def emit(self, event) -> HookOutcome:
        """Dispatch ``event`` to every subscriber in order; fold their outcomes.

        Observation callers ignore the return; control callers read it
        (veto/mutate/stop). One subscriber raising is logged and skipped so the
        stream never breaks.
        """
        outcomes: List[HookOutcome] = []
        for sub in self._subs:
            try:
                out = await sub.handle(event)
            except Exception as exc:  # noqa: BLE001 — one bad sub never breaks the spine
                logger.warning(
                    f"EventBus: subscriber {type(sub).__name__} raised on "
                    f"{getattr(event, 'name', '?')}: {exc}"
                )
                continue
            if out:
                outcomes.append(out)
        if not outcomes:
            return EMPTY
        return fold(outcomes)

    def emit_sync(self, event) -> None:
        """Fire-and-forget delivery to sync-capable subscribers only.

        For observation events raised from synchronous call sites (e.g. a tool
        capturing a file snapshot before writing). Subscribers that expose a
        ``handle_sync(event)`` method receive it; others are skipped. Never
        raises, never returns an outcome.
        """
        for sub in self._subs:
            handler = getattr(sub, "handle_sync", None)
            if handler is None:
                continue
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"EventBus: subscriber {type(sub).__name__} raised (sync) on "
                    f"{getattr(event, 'name', '?')}: {exc}"
                )


__all__ = ["EventBus"]
