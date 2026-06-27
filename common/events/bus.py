"""EventBus — the single ordered async stream every producer/consumer shares.

Every event is dispatched in **two phases**, giving each plane the contract it
actually needs instead of forcing one policy on both:

* **Phase 1 — control.** :class:`ControlSubscriber`\\s (normally just the hook
  subscriber) are awaited *inline, in priority order*, and their
  :class:`HookOutcome`\\s are folded into the value the emitter reads. Inline +
  serial is mandatory here: a veto must be folded *before* the emitter proceeds
  (a denied tool call is folded before the recorder ever sees it). Each handler
  is time-boxed so a hung hook cannot freeze the agent forever.

* **Phase 2 — observation.** :class:`ObservationSubscriber`\\s (recorder,
  renderer, logger, tracing) are fanned out, *isolated per subscriber*. Their
  return is ignored — an observer can never veto, by construction. Delivery is
  graded by each subscriber's :data:`DeliveryPolicy`:

    - ``MIRROR`` (default): best-effort + time-boxed. A slow or failing mirror is
      dropped and counted; it never stalls or breaks the turn.
    - ``DURABLE`` (the rollout recorder): no timeout (it must complete), and a
      failure is surfaced — logged loud and counted in :attr:`durable_failures`,
      never silently swallowed — because a missing rollout record is data loss.

``emit`` runs both phases and returns the folded outcome. ``observe`` runs phase
2 only (the transport for fire-and-forget observation events raised from deep
call sites via the active-bus contextvar — it structurally cannot carry control,
so losing the contextvar in a spawned task can only drop an observation, never a
veto). ``emit_sync`` delivers observation events from synchronous call sites to
subscribers exposing ``handle_sync``.

Leaf module: imports only ``common.events`` siblings + ``common.logs`` + the
subscriber protocols. It never imports roles/context/executor — those inject
themselves as subscribers.
"""

from __future__ import annotations

import asyncio
from typing import List

from metagpt.common.events.outcome import EMPTY, HookOutcome, fold
from metagpt.common.interface.event_subscriber import (
    DURABLE,
    ControlSubscriber,
    ObservationSubscriber,
)
from metagpt.common.logs import logger

#: Per-subscriber wall-clock budgets (circuit breakers, not tight SLAs). A
#: handler exceeding these is abandoned with a warning so one wedged subscriber
#: never freezes the spine. Control gets a generous budget (hooks may shell out);
#: mirror observers a tighter one. ``DURABLE`` observers are never timed out.
DEFAULT_CONTROL_TIMEOUT = 120.0
DEFAULT_OBSERVER_TIMEOUT = 30.0


def _insert_by_priority(subs: list, sub) -> None:
    """Insert ``sub`` keeping ``subs`` sorted by ascending ``priority`` (stable)."""
    priority = getattr(sub, "priority", 0)
    idx = len(subs)
    for i, existing in enumerate(subs):
        if getattr(existing, "priority", 0) > priority:
            idx = i
            break
    subs.insert(idx, sub)


class EventBus:
    """A two-plane fan-out: control subscribers fold, observers mirror."""

    def __init__(
        self,
        *,
        control_timeout: float = DEFAULT_CONTROL_TIMEOUT,
        observer_timeout: float = DEFAULT_OBSERVER_TIMEOUT,
    ) -> None:
        self._control: List[ControlSubscriber] = []
        self._observers: List[ObservationSubscriber] = []
        self._control_timeout = control_timeout
        self._observer_timeout = observer_timeout
        #: Count of durable-sink delivery failures (a non-zero value means the
        #: rollout may be incomplete — surfaced for health checks).
        self.durable_failures: int = 0

    # -- registration -------------------------------------------------------

    def subscribe(self, sub) -> None:
        """Register ``sub`` on the plane it implements (ascending priority).

        A subscriber exposing ``handle_control`` joins the control plane; every
        other subscriber is an observer. Stable for equal priorities.
        """
        target = self._control if hasattr(sub, "handle_control") else self._observers
        _insert_by_priority(target, sub)

    def unsubscribe(self, sub) -> None:
        """Remove ``sub`` from whichever plane holds it (safe no-op otherwise)."""
        for plane in (self._control, self._observers):
            try:
                plane.remove(sub)
                return
            except ValueError:
                continue

    @property
    def subscribers(self) -> list:
        """All subscribers in dispatch order: control plane, then observers."""
        return list(self._control) + list(self._observers)

    # -- dispatch -----------------------------------------------------------

    async def emit(self, event) -> HookOutcome:
        """Dispatch ``event`` through both phases; return the folded outcome.

        Phase 1 (control) folds an influence the caller acts on; phase 2
        (observation) is fire-and-forget. Pure-observation events simply produce
        an :data:`EMPTY` outcome (no control subscriber maps them).
        """
        outcome = await self._run_control(event)
        await self._dispatch_observers(event)
        return outcome

    async def observe(self, event) -> None:
        """Run phase 2 only — fan out ``event`` to observers, no control, no fold.

        The transport for observation events raised from deep call sites; it
        cannot influence the host.
        """
        await self._dispatch_observers(event)

    def emit_sync(self, event) -> None:
        """Fire-and-forget delivery to observers exposing ``handle_sync``.

        For observation events raised from synchronous call sites (e.g. a tool
        capturing a file snapshot before writing). Never raises, never folds.
        """
        for sub in self._observers:
            handler = getattr(sub, "handle_sync", None)
            if handler is None:
                continue
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"EventBus: observer {type(sub).__name__} raised (sync) on "
                    f"{getattr(event, 'name', '?')}: {exc}"
                )

    # -- internals ----------------------------------------------------------

    async def _run_control(self, event) -> HookOutcome:
        """Phase 1: await control subscribers inline, fold their outcomes."""
        outcomes: List[HookOutcome] = []
        for sub in self._control:
            try:
                out = await asyncio.wait_for(
                    sub.handle_control(event), self._control_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"EventBus: control subscriber {type(sub).__name__} timed out "
                    f"(>{self._control_timeout}s) on {getattr(event, 'name', '?')}; "
                    "treating as no-outcome"
                )
                continue
            except Exception as exc:  # noqa: BLE001 — one bad sub never breaks the spine
                logger.warning(
                    f"EventBus: control subscriber {type(sub).__name__} raised on "
                    f"{getattr(event, 'name', '?')}: {exc}"
                )
                continue
            if out:
                outcomes.append(out)
        if not outcomes:
            return EMPTY
        return fold(outcomes)

    async def _dispatch_observers(self, event) -> None:
        """Phase 2: fan out to observers, isolated and graded by delivery policy."""
        for sub in self._observers:
            policy = getattr(sub, "delivery", None) or "mirror"
            try:
                if policy == DURABLE:
                    await sub.handle(event)  # must complete; not time-boxed
                else:
                    await asyncio.wait_for(sub.handle(event), self._observer_timeout)
            except asyncio.TimeoutError:
                if policy == DURABLE:
                    # Unreachable (durable is not time-boxed) but kept defensive.
                    self.durable_failures += 1
                    logger.error(
                        f"EventBus: DURABLE sink {type(sub).__name__} timed out on "
                        f"{getattr(event, 'name', '?')} — rollout may be incomplete"
                    )
                else:
                    logger.warning(
                        f"EventBus: mirror {type(sub).__name__} timed out "
                        f"(>{self._observer_timeout}s) on {getattr(event, 'name', '?')}; dropped"
                    )
            except Exception as exc:  # noqa: BLE001 — isolation; the turn never breaks
                if policy == DURABLE:
                    self.durable_failures += 1
                    logger.error(
                        f"EventBus: DURABLE sink {type(sub).__name__} failed on "
                        f"{getattr(event, 'name', '?')}: {exc} — rollout may be incomplete"
                    )
                else:
                    logger.warning(
                        f"EventBus: mirror {type(sub).__name__} raised on "
                        f"{getattr(event, 'name', '?')}: {exc}"
                    )


__all__ = ["EventBus", "DEFAULT_CONTROL_TIMEOUT", "DEFAULT_OBSERVER_TIMEOUT"]
