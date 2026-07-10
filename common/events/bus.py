"""EventBus — the single ordered async stream every producer/consumer shares.

Every event is dispatched in **two phases**, giving each plane the contract it
actually needs instead of forcing one policy on both:

* **Phase 1 — control.** :class:`ControlSubscriber`\\s (e.g. the hook subscriber
  and the permission gate) are **routed by event name** into per-event buckets:
  each subscriber declares the events it ``handles``, so an event is delivered
  only to the subscribers interested in it (no global list every subscriber must
  scan). Within a bucket, subscribers run *inline* ordered by :class:`ControlStage`
  as a **chained reduce**: each subscriber's :class:`ControlOutcome` is folded
  into the running result via the outcome's own ``merge``, and if it rewrote the
  call the rewrite is *threaded forward* (via the outcome's ``rebind``) so the
  next subscriber observes the already-rewritten event. Inline + serial is
  mandatory here: a veto must be folded *before* the emitter proceeds (a denied
  tool call is folded before the recorder ever sees it). A blocking outcome
  (deny/stop) short-circuits the remaining subscribers. Each handler is
  time-boxed; how a *failure* (crash or timeout) is treated is the subscriber's
  :data:`FailMode`:

    - ``FAIL_OPEN`` (default): the failed subscriber contributes no outcome and
      the chain continues — correct for advisory hooks (a broken hook must never
      brick the agent).
    - ``FAIL_CLOSED``: the failure *denies* the call and short-circuits — correct
      for the security gate, where "could not decide" must fail safe. Because the
      bus is generic (it does not know which outcome type a given event uses), a
      fail-closed subscriber supplies ``on_failure(reason)`` returning the correct
      typed deny for the bus to fold.

* **Phase 2 — observation.** :class:`ObservationSubscriber`\\s (recorder,
  renderer, logger, tracing) are fanned out, *isolated per subscriber*. Their
  return is ignored — an observer can never veto, by construction. Delivery is
  graded by each subscriber's :data:`DeliveryPolicy`:

    - ``MIRROR`` (default): best-effort + time-boxed. A slow or failing mirror is
      dropped and counted; it never stalls or breaks the turn.
    - ``DURABLE`` (the rollout recorder): no timeout (it must complete), and a
      failure is surfaced — logged loud and counted in :attr:`durable_failures`,
      never silently swallowed — because a missing rollout record is data loss.

``emit`` runs both phases and returns the folded outcome (an
:class:`~metagpt.common.interface.event_subscriber.ControlOutcome`, or ``None``
when no control subscriber maps the event). ``observe`` runs phase 2 only (the
transport for fire-and-forget observation events raised from deep call sites via
the active-bus contextvar — it structurally cannot carry control, so losing the
contextvar in a spawned task can only drop an observation, never a veto).
``emit_sync`` delivers observation events from synchronous call sites to
subscribers exposing ``handle_sync``.

Leaf module: imports only ``common.logs`` + the subscriber protocols. It never
imports roles/context/executor — those inject themselves as subscribers — nor
any concrete outcome type (it drives them through the ``ControlOutcome``
protocol), so a new event's outcome needs no change here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Dict, List, Optional

from metagpt.common.interface.event_subscriber import (
    DURABLE,
    FAIL_CLOSED,
    BusAware,
    ControlOutcome,
    ControlSubscriber,
    ObservationSubscriber,
    SyncObserver,
)
from metagpt.common.logs import logger

#: Per-subscriber wall-clock budgets (circuit breakers, not tight SLAs). A
#: handler exceeding these is abandoned with a warning so one wedged subscriber
#: never freezes the spine. Control gets a generous budget (hooks may shell out);
#: mirror observers a tighter one. ``DURABLE`` observers are never timed out.
DEFAULT_CONTROL_TIMEOUT = 120.0
DEFAULT_OBSERVER_TIMEOUT = 30.0


def _insert_by(subs: list, sub, key) -> None:
    """Insert ``sub`` keeping ``subs`` sorted by ascending ``key(sub)`` (stable)."""
    rank = key(sub)
    idx = len(subs)
    for i, existing in enumerate(subs):
        if key(existing) > rank:
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
        #: Control subscribers routed by the event *name* they handle; each bucket
        #: is kept sorted by ``ControlStage`` so a shared bucket runs in causal
        #: order (rewrite before gate). A bucket is usually a single subscriber.
        self._control: Dict[str, List[ControlSubscriber]] = defaultdict(list)
        self._observers: List[ObservationSubscriber] = []
        self._control_timeout = control_timeout
        self._observer_timeout = observer_timeout
        #: Count of durable-sink delivery failures (a non-zero value means the
        #: rollout may be incomplete — surfaced for health checks).
        self.durable_failures: int = 0

    # -- registration -------------------------------------------------------

    def subscribe(self, sub) -> None:
        """Register ``sub`` on the plane it *declares* by inheritance.

        A :class:`ControlSubscriber` joins the control plane, filed into each
        named bucket it ``handles`` ordered by ``ControlStage`` (its non-empty
        ``handles`` and, for a fail-closed gate, its ``on_failure`` are already
        enforced at class-definition time by ``__init_subclass__``). An
        :class:`ObservationSubscriber` is filed into one fan-out list ordered by
        ``priority``. Anything that is neither raises ``TypeError`` — a subscriber
        must declare its plane.

        A subscriber that is also a *producer* (it emits back onto the bus)
        inherits :class:`BusAware`, whose ``on_subscribed(bus)`` lifecycle hook is
        invoked once on registration, handing it its own bus handle. This
        generalizes the observer-that-also-emits pattern (e.g. the LSP service
        consumes FileMutated and re-emits Diagnostics) so the host need not
        special-case a back-reference, keeping the bus a leaf (it never imports
        the producer).
        """
        if isinstance(sub, ControlSubscriber):
            for name in sub.handles:
                _insert_by(self._control[name], sub, lambda s: s.stage)
        elif isinstance(sub, ObservationSubscriber):
            _insert_by(self._observers, sub, lambda s: s.priority)
        else:
            raise TypeError(
                f"{type(sub).__name__} is neither a ControlSubscriber nor an "
                "ObservationSubscriber; a subscriber must declare its plane by inheritance"
            )
        if isinstance(sub, BusAware):
            sub.on_subscribed(self)

    def unsubscribe(self, sub) -> None:
        """Remove ``sub`` from whichever plane holds it (safe no-op otherwise)."""
        if isinstance(sub, ControlSubscriber):
            for bucket in self._control.values():
                try:
                    bucket.remove(sub)
                except ValueError:
                    continue
            return
        try:
            self._observers.remove(sub)
        except ValueError:
            pass

    @property
    def subscribers(self) -> list:
        """All subscribers in dispatch order: control plane (deduped), then observers.

        A control subscriber filed into several buckets appears once, in
        first-seen order across buckets.
        """
        seen: list = []
        for bucket in self._control.values():
            for sub in bucket:
                if sub not in seen:
                    seen.append(sub)
        return seen + list(self._observers)

    # -- dispatch -----------------------------------------------------------

    async def emit(self, event) -> Optional[ControlOutcome]:
        """Dispatch ``event`` through both phases; return the folded outcome.

        Phase 1 (control) folds an influence the caller acts on; phase 2
        (observation) is fire-and-forget. Pure-observation events (no control
        subscriber maps their name) return ``None`` — the caller simply does not
        read an outcome for them.

        When phase 1 rewrote the call, observers receive the **final rewritten**
        event, so what is recorded/rendered matches what actually runs.
        """
        outcome, final_event = await self._run_control(event)
        await self._dispatch_observers(final_event)
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
            if not isinstance(sub, SyncObserver):
                continue
            try:
                sub.handle_sync(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"EventBus: observer {type(sub).__name__} raised (sync) on "
                    f"{getattr(event, 'name', '?')}: {exc}"
                )

    # -- internals ----------------------------------------------------------

    async def _run_control(self, event):
        """Phase 1: await this event's bucket inline as a chained reduce.

        Returns ``(folded_outcome, final_event)``. Only subscribers that declared
        the event's name run, ordered by stage. Each subscriber's outcome is
        folded into the accumulator via ``merge`` and its rewrite threaded forward
        via ``rebind`` (so subscriber *i+1* sees the event as rewritten by *i*),
        stamped with the subscriber's name as provenance; a blocking outcome
        (deny/stop) short-circuits the rest. A subscriber that
        crashes/times out is handled per its :data:`FailMode`: ``FAIL_OPEN`` drops
        its contribution and continues; ``FAIL_CLOSED`` folds its ``on_failure``
        typed deny and short-circuits (fail-safe for security gates). Returns
        ``(None, event)`` when nothing maps the event (pure observation).
        """
        acc: Optional[ControlOutcome] = None
        current = event
        for sub in self._control.get(event.name, ()):
            try:
                out = await asyncio.wait_for(sub.handle_control(current), self._control_timeout)
                if out is None:
                    continue
                # A subscriber must return the event's bound outcome type — a
                # wrong type is a contract violation, not a silent no-op. This
                # runs *inside* the try so it is routed through fail_mode like
                # any handler crash (HA: a malformed outcome cannot crash the turn).
                if not isinstance(out, event.outcome_type):
                    raise TypeError(
                        f"{type(sub).__name__} returned {type(out).__name__} for "
                        f"{event.name}, expected {event.outcome_type.__name__}"
                    )
                # Compute every outcome operation *before* committing anything, so
                # a bad merge / non-Rewritable rebind is contained (not a partial
                # commit that could double-merge). ``by`` = the subscriber's name
                # (its stable label, else the class name) — the single point that
                # knows both the subscriber and the outcome it just produced.
                folded = out if acc is None else acc.merge(out)
                rebound = out.rebind(current, by=(sub.name or type(sub).__name__))
                blocking = out.is_blocking
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                out = self._on_control_failure(sub, current, exc)
                if out is None:
                    continue  # fail-open: dropped, chain continues
                acc = out if acc is None else acc.merge(out)
                break  # fail-closed: folded a deny, short-circuit
            # Atomic commit: only after all outcome ops succeeded.
            acc, current = folded, rebound
            # A deny/stop is final — no later subscriber can un-block it.
            if blocking:
                break
        return acc, current

    def _on_control_failure(self, sub, event, exc) -> Optional[ControlOutcome]:
        """Resolve a crashed/timed-out control subscriber per its ``fail_mode``.

        ``FAIL_OPEN`` → ``None`` (drop its influence, continue). ``FAIL_CLOSED`` →
        the subscriber's own ``on_failure(reason)`` typed deny (the bus cannot
        build it — it does not know the event's outcome type).
        """
        timed_out = isinstance(exc, asyncio.TimeoutError)
        what = f"timed out (>{self._control_timeout}s)" if timed_out else f"raised: {exc}"
        name = getattr(event, "name", "?")
        if sub.fail_mode == FAIL_CLOSED:
            logger.error(
                f"EventBus: control gate {type(sub).__name__} {what} on {name}; "
                "failing closed (deny)"
            )
            reason = (
                f"{type(sub).__name__} could not evaluate the request "
                f"({'timeout' if timed_out else 'error'}); denied for safety."
            )
            return sub.on_failure(reason)
        logger.warning(
            f"EventBus: control subscriber {type(sub).__name__} {what} on {name}; "
            "treating as no-outcome"
        )
        return None

    async def _dispatch_observers(self, event) -> None:
        """Phase 2: fan out to observers, isolated and graded by delivery policy."""
        for sub in self._observers:
            policy = sub.delivery
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
