"""Loop-guard subsystem — repeated-failure / no-progress tool-call detection.

Public surface:
  * :class:`ThrashDetector` — the pure per-Role streak state machine.
  * :class:`Verdict` — a tripped streak (what the subscriber turns into a nudge).
  * :class:`LoopGuardSubscriber` — the PostToolUse control subscriber that folds
    a verdict into an in-band nudge on the finished call's result.

No module in this package imports a concrete tool or the executor: the subscriber
takes ``resolve_readonly`` / ``sig_of`` closures the executor supplies, mirroring
how the permission engine stays tool-free behind ``resolve_facts``.
"""
from __future__ import annotations

from mote.executor.loop_guard.detector import ThrashDetector, Verdict
from mote.executor.loop_guard.subscriber import LoopGuardSubscriber

__all__ = ["ThrashDetector", "Verdict", "LoopGuardSubscriber"]
