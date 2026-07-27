"""TemporalBackend — the Tier-2 :class:`DurableBackend` over Temporal activities.

Satisfies the SAME :class:`~mote.runtime.durable.backend.DurableBackend` protocol the
loop drives for Tier 1, so the loop's typed façades
(:class:`~mote.runtime.durable.think_journal.ThinkJournal`) are backend-agnostic — only the
transport of ``run_step`` differs:

* **Inside a workflow** (``workflow.in_workflow()``) — dispatch the step as a
  Temporal activity (``workflow.execute_activity``) whose result the event history
  memoizes. The step's closure is registered process-locally first (see
  :mod:`._activities`), then the serializable :class:`StepInput` descriptor is
  sent to the activity. Retry / timeout come from the per-seam
  :class:`~mote.contracts.schema.ActivityConfig` mapped onto temporalio's
  :class:`RetryPolicy`.
* **Outside a workflow** (a plain resume, a unit test, or the loop driven without
  a worker) — run the closure INLINE and record to the shared journal directly,
  byte-for-byte the JSONL tier's behaviour. This keeps the backend usable
  everywhere the loop runs, and is the path the ledger precheck (EXTERNAL
  idempotency) already covers.

Either way the SHARED :class:`RunJournal` is the durable record both tiers agree
on — the Temporal tier does not replace it, it drives the same ledger from inside
its activities (belt-and-suspenders idempotency).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Awaitable, Callable, Optional

try:
    from temporalio import workflow
    from temporalio.common import RetryPolicy
except ImportError:  # optional durable backend
    workflow = None
    RetryPolicy = None

from mote.contracts.schema import ActivityConfig, TemporalConfig
from mote.runtime.durable.temporal._activities import RUN_STEP_ACTIVITY, StepActivities, StepHandlerRegistry, StepInput
from mote.runtime.ledger import KIND_THINK, KIND_TIMER, RunJournal


def _retry_policy(cfg: ActivityConfig):
    """Map a per-seam :class:`ActivityConfig` onto temporalio's ``RetryPolicy``.

    ``max_retry_attempts=0`` (the mote default) means UNBOUNDED attempts —
    temporalio spells that ``maximum_attempts=0`` too, so it passes straight
    through. ``non_retryable_error_types`` names mote's user-logic error classes
    that must never be retried (a bad tool arg won't succeed on retry).
    """
    if RetryPolicy is None:
        raise RuntimeError("Temporal backend requires the 'temporalio' extra")
    return RetryPolicy(
        initial_interval=timedelta(seconds=cfg.initial_retry_interval_seconds),
        backoff_coefficient=cfg.retry_backoff_coefficient,
        maximum_interval=(
            timedelta(seconds=cfg.max_retry_interval_seconds) if cfg.max_retry_interval_seconds is not None else None
        ),
        maximum_attempts=cfg.max_retry_attempts,
        non_retryable_error_types=list(cfg.non_retryable_error_types),
    )


def _activity_kwargs(cfg: ActivityConfig) -> dict:
    """Build the ``workflow.execute_activity`` keyword args from a seam config.

    A ``start_to_close_timeout`` is REQUIRED by Temporal for every activity; when
    the seam leaves it unset we fall back to a generous default (an LLM think turn
    can legitimately run for minutes) rather than forcing config on every caller.
    """
    timeout = cfg.start_to_close_timeout_seconds
    return {
        "start_to_close_timeout": timedelta(seconds=timeout if timeout is not None else 600.0),
        "retry_policy": _retry_policy(cfg),
    }


class TemporalBackend:
    """Tier-2 durable backend dispatching steps as Temporal activities.

    Holds the shared journal, the process-local handler registry, the bound
    :class:`StepActivities`, and the per-seam activity policy resolved from
    :class:`TemporalConfig`. The loop builds ONE per run (mirroring how it builds
    one :class:`JsonlBackend`).
    """

    def __init__(self, config: TemporalConfig, journal: RunJournal) -> None:
        self._config = config
        self._journal = journal
        self._registry = StepHandlerRegistry()
        self._activities = StepActivities(journal, self._registry)

    @property
    def journal(self) -> RunJournal:
        """The shared run journal both tiers memoize steps into."""
        return self._journal

    @property
    def config(self) -> TemporalConfig:
        """The Temporal wiring config (server/namespace/task-queue + seam policy)."""
        return self._config

    @property
    def temporal_activities(self) -> list:
        """The activity functions a worker must register for this backend."""
        return [self._activities.run_step_activity]

    def _seam_config(self, kind: str) -> ActivityConfig:
        """The per-seam :class:`ActivityConfig` for a step ``kind``.

        think→think_activity, timer→timer_activity, everything else (tool) →
        tool_activity. A single lookup so a new kind falls back to the tool
        policy rather than crashing.
        """
        if kind == KIND_THINK:
            return self._config.think_activity
        if kind == KIND_TIMER:
            return self._config.timer_activity
        return self._config.tool_activity

    async def run_step(
        self,
        step_id: str,
        kind: str,
        effect: str,
        execute: Callable[[], Awaitable[str]],
        *,
        name: str = "",
        seq: int = 0,
        tool_call_id: Optional[str] = None,
    ) -> str:
        """Memoize one step via a Temporal activity (in-workflow) or inline.

        Registers the closure process-locally, then either dispatches the
        activity (inside a workflow — Temporal memoizes the result) or runs it
        inline against the shared journal (outside a workflow — same behaviour as
        the JSONL tier). The ``StepInput`` descriptor carries only the step's
        identity across the activity boundary.
        """
        self._registry.register(step_id, execute)
        step = StepInput(
            step_id=step_id,
            kind=kind,
            effect=effect,
            name=name,
            seq=seq,
            tool_call_id=tool_call_id,
        )

        if workflow is None:
            raise RuntimeError("Temporal backend requires the 'temporalio' extra")
        if not workflow.in_workflow():
            # Outside a workflow (resume / test / worker-less loop): run inline,
            # recording to the journal exactly as the activity would. The inline
            # impl does the ``completed`` short-circuit + journal I/O itself.
            return await self._activities.run_step(step)

        # Inside a workflow: do NO journal I/O here (workflow code must stay
        # deterministic + side-effect-free). The ``completed`` short-circuit is
        # REDUNDANT on this path — Temporal's event history already memoizes a
        # finished activity's result, so on replay ``execute_activity`` returns
        # the recorded payload WITHOUT re-running the activity (which is where
        # the journal read/write lives). Retry / timeout come from the seam's
        # ActivityConfig.
        return await workflow.execute_activity(
            RUN_STEP_ACTIVITY,
            step,
            **_activity_kwargs(self._seam_config(kind)),
        )


__all__ = ["TemporalBackend"]
