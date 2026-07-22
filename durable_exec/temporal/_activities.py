"""Temporal activity seam — the ONE generic durable step, run as an activity.

The architectural crux (verified against pydantic-ai's ``durable_exec/temporal``):
a Temporal **activity is a registered named function taking SERIALIZABLE args**,
never an arbitrary closure. mote's :class:`~mote.loop.durable.backend.DurableBackend`
seam is closure-based (``run_step(execute=...)``) so the two worlds are bridged
with a small, deliberate design:

* A serializable :class:`StepInput` descriptor carries the step's IDENTITY
  (``step_id``/``kind``/``effect``/``name``/``seq``/``tool_call_id``) across the
  activity boundary — everything the journal needs to record a step.
* The step's actual work (the closure that re-pays the LLM / re-runs the tool /
  waits the timer) CANNOT be serialized, so it stays in-process: the backend
  registers each closure in a process-local :class:`StepHandlerRegistry` keyed by
  ``step_id`` before dispatching the activity, and the activity looks it up. This
  is sound because a Temporal WORKER runs activities in the SAME process that
  registered them (the worker hosts both the workflow and its activities); on a
  crash-replay Temporal SKIPS a completed activity entirely (its result comes
  from event history), so a lost registry entry after a restart is never needed
  for an already-completed step.
* The journal write (disk I/O) lives INSIDE the activity, never in workflow code
  (workflow code must stay deterministic / I/O-free). The activity records
  ``started`` → runs the handler → records the terminal via the shared
  :func:`~mote.common.ledger.run_journaled_step` body :class:`JsonlBackend` also
  uses (identical by construction). This is ALSO the EXTERNAL-effect
  belt-and-suspenders: even though Temporal memoizes the activity result, the
  same ledger precheck runs here so a duplicate side effect is impossible if the
  activity is ever retried before its result is committed.

The activity RETURNS the payload string (opaque, same contract as the JSONL
tier); Temporal's pydantic data converter serializes the ``StepInput`` in and the
``str`` out.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from pydantic import BaseModel
from temporalio import activity

from mote.common.ledger import COMPLETED, RunJournal, run_journaled_step

#: The closure type the backend registers per step (same as ``run_step``'s
#: ``execute``). Not serializable — kept process-local in the registry below.
StepExecute = Callable[[], Awaitable[str]]

#: The Temporal activity name for the one generic durable step. A single
#: activity handles every step KIND (think/tool/timer) because the kind is data
#: on the descriptor, not a separate code path — mirrors the JSONL tier's one
#: ``run_step``.
RUN_STEP_ACTIVITY = "mote__run_step"


class StepInput(BaseModel):
    """Serializable descriptor of one durable step (the activity's input).

    Carries only the step's IDENTITY — the journal-recording facts. The work
    itself (the closure) is looked up process-locally by ``step_id`` (see module
    docstring), so it never needs to cross the wire.
    """

    step_id: str
    kind: str
    effect: str
    name: str = ""
    seq: int = 0
    tool_call_id: Optional[str] = None


class StepHandlerRegistry:
    """Process-local map ``step_id -> execute`` bridging the closure boundary.

    The backend registers a step's closure here right before dispatching the
    activity; the activity (running in the SAME worker process) looks it up. A
    completed step is never re-run on replay (Temporal serves its result from
    event history), so an entry only needs to outlive the single activity
    execution that consumes it — :meth:`pop` removes it once run.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, StepExecute] = {}

    def register(self, step_id: str, execute: StepExecute) -> None:
        self._handlers[step_id] = execute

    def pop(self, step_id: str) -> Optional[StepExecute]:
        return self._handlers.pop(step_id, None)

    def clear(self) -> None:
        self._handlers.clear()


class StepActivities:
    """The activity implementation, bound to ONE journal + handler registry.

    An instance holds the per-run journal and registry; :attr:`run_step_activity`
    is the ``@activity.defn`` the worker registers. Kept a class (not a bare
    function) so the journal/registry are injected rather than global — one
    instance per backend, so parallel runs never share a registry.
    """

    def __init__(self, journal: RunJournal, registry: StepHandlerRegistry) -> None:
        self._journal = journal
        self._registry = registry

        # ``activity.defn`` must decorate a plain function (it sets an attribute
        # on the callable — a bound method is read-only), so the activity is a
        # nested closure over this instance's journal/registry, mirroring how
        # pydantic-ai builds its per-instance activities. Delegates to
        # :meth:`run_step` so the inline (non-workflow) path shares one impl.
        async def run_step_activity(step: StepInput) -> str:
            return await self.run_step(step)

        self.run_step_activity = activity.defn(name=RUN_STEP_ACTIVITY)(run_step_activity)

    async def run_step(self, step: StepInput) -> str:
        """Record + run one durable step via the shared journaled-step body.

        A ``completed`` record short-circuits (skip the handler, replay the
        recorded payload) — this is the EXTERNAL-effect idempotency guard that
        holds even though Temporal already memoizes the activity result. Any
        other prior state (none / ``started`` / ``failed``) resolves the
        process-local handler then delegates to
        :func:`~mote.common.ledger.run_journaled_step`, the ONE side-effecting
        body :class:`JsonlBackend` also uses — so the two tiers stay identical
        by construction, not by a hand-maintained mirror.
        """
        prior = self._journal.replay(step.step_id)
        if prior is not None and prior.status == COMPLETED:
            return prior.payload or ""
        execute = self._registry.pop(step.step_id)
        if execute is None:
            # No handler registered for this step in THIS process — a resume in a
            # fresh worker where the closure was never re-registered. A completed
            # step is served above from history; reaching here means the step is
            # genuinely un-runnable, so record failed and surface it.
            self._journal.record_failed(step.step_id, payload="no step handler registered")
            raise RuntimeError(f"no durable step handler registered for {step.step_id!r}")
        return await run_journaled_step(
            self._journal,
            step.step_id,
            step.kind,
            step.effect,
            execute,
            name=step.name,
            seq=step.seq,
            tool_call_id=step.tool_call_id,
        )


__all__ = [
    "StepInput",
    "StepHandlerRegistry",
    "StepActivities",
    "StepExecute",
    "RUN_STEP_ACTIVITY",
]
