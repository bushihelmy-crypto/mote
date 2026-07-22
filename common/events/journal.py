"""Bridge a durable run-journal step lifecycle onto the event bus.

The journal primitives (:class:`~mote.common.ledger.RunJournal` and its
tool-facing / think-facing / timer views) are bus-agnostic leaves that know
nothing of events — they just append durable records. This module is the single
glue seam that turns a step-lifecycle transition into a
:class:`JournalEvent` observation on the active bus.

Fire-and-forget on the active bus; a no-op when no bus is bound (so a journal
write outside a runtime scope — e.g. a resume reconcile in a bare tool — never
raises). Callers pass the plain wire strings the record already carries, so this
seam imports nothing from :mod:`~mote.common.ledger` (the layering stays
``ledger`` ⟂ ``events``, verified: neither imports the other).
"""

from __future__ import annotations

from .context import observe_event_sync
from .types import JournalEvent

#: The four lifecycle transitions a journal step can announce (wire-stable).
STARTED = "started"
COMPLETED = "completed"
FAILED = "failed"
REAPED = "reaped"


def emit_journal_event(step_id: str, kind: str, phase: str, *, effect: str = "", seq: int = 0) -> None:
    """Mirror one journal step-lifecycle transition onto the active bus.

    Observation only — the journal's on-disk log stays the source of truth; this
    just announces *that* a record moved so a frontend/logger can watch the
    crash-resume bookkeeping. No-op when no bus is bound.
    """
    observe_event_sync(JournalEvent(step_id=step_id, kind=kind, phase=phase, effect=effect, seq=seq))


__all__ = ["emit_journal_event", "STARTED", "COMPLETED", "FAILED", "REAPED"]
