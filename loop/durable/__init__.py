"""Durable-execution backends — the neutral seam both tiers hang on.

Tier 1 (:class:`JsonlBackend`, always-on, zero-dependency) memoizes a run's
replay-safe steps in the shared :class:`~mote.common.ledger.RunJournal`; Tier 2
(the optional Temporal backend, ``durable_exec/temporal/``) dispatches the same
steps as activities. Both satisfy the :class:`DurableBackend` protocol, so the
loop drives ONE control plane and only the transport differs.

:class:`DurableRunner` is the first typed seam façade over the backend (A3): it
memoizes the think turn so a resume reinstates its result instead of re-paying
the model.
"""

from mote.loop.durable.backend import DurableBackend, JsonlBackend
from mote.loop.durable.factory import make_durable_backend
from mote.loop.durable.runner import (
    DurableRunner,
    assistant_message_present,
    begin_timer,
    complete_timer,
    reconcile_think_journal,
    resume_timer,
)

__all__ = [
    "DurableBackend",
    "JsonlBackend",
    "make_durable_backend",
    "DurableRunner",
    "assistant_message_present",
    "reconcile_think_journal",
    "begin_timer",
    "resume_timer",
    "complete_timer",
]
