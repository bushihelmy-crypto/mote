"""Subscriber protocols — the two planes the :class:`EventBus` fans out to.

The bus dispatches every event in **two phases**, and which phase a subscriber
runs in is a structural property of the subscriber, not a flag on the event:

* :class:`ControlSubscriber` (phase 1) — awaited inline. It declares which events
  it ``handles`` (by name) so the bus routes each event only to its interested
  subscribers, and a :class:`ControlStage` that orders the few subscribers that
  share a bucket. Its ``handle_control`` may return a :class:`ControlOutcome`
  which the bus *folds* (via the outcome's own ``merge``) and hands back to the
  emitter (veto / mutate args / inject context / stop). Because only this plane
  folds, influence over the host is confined to subscribers registered here.

* :class:`ObservationSubscriber` (phase 2) — fan-out sinks (recorder, renderer,
  logger, tracing). ``handle`` returns nothing the bus reads: an observer can
  **never** veto, by construction. Each declares a :data:`DeliveryPolicy`:

    - ``MIRROR`` (default): best-effort, isolated, time-boxed — a slow or failing
      mirror is dropped+counted and never stalls or breaks the turn.
    - ``DURABLE``: the rollout recorder — failures are surfaced (logged loud +
      counted on the bus), not silently swallowed, because a missing rollout
      record is real data loss.

  An optional synchronous ``handle_sync`` receives fire-and-forget observation
  events emitted from sync call sites (see ``EventBus.emit_sync``). Not required.

The bus classifies a subscriber by capability: anything exposing
``handle_control`` joins the control plane; everything else is an observer. This
is why there is no ``is_control`` marker on events — the plane is decided by
*where a subscriber is registered*, and enforced by which phase can fold.

Leaf module: imports only ``typing`` (protocols are structural).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal, Optional, Protocol, runtime_checkable

#: How the observation plane delivers an event to a subscriber.
DeliveryPolicy = Literal["mirror", "durable"]
MIRROR: DeliveryPolicy = "mirror"
DURABLE: DeliveryPolicy = "durable"

#: What the control plane does when a subscriber crashes or times out. The
#: control-plane analogue of :data:`DeliveryPolicy`:
#:   - ``open`` (default): a failed subscriber is treated as *no influence* — its
#:     outcome is dropped and the turn proceeds. Correct for advisory subscribers
#:     (the hook layer) where a broken hook must not brick the agent.
#:   - ``closed``: a failed subscriber *denies* the call. Correct for
#:     security-critical gates (the permission engine) where "we could not decide"
#:     must fail safe, not fail permissive.
FailMode = Literal["open", "closed"]
FAIL_OPEN: FailMode = "open"
FAIL_CLOSED: FailMode = "closed"


class ControlStage(IntEnum):
    """Ordering within one control bucket — a *named* contract, not a magic int.

    Control subscribers are routed by event name into per-event buckets; the vast
    majority of buckets hold a single subscriber, for which the stage is
    immaterial. The only shared bucket today is ``PreToolUse`` — the hook rewrites
    args (:attr:`REWRITE`), then the permission gate evaluates the *already
    rewritten* call (:attr:`GATE`) — so ``REWRITE < GATE`` encodes that causal
    dependency by name rather than by comparing bare integers. A future ordered
    bucket adds a named member here (one line, with meaning) instead of guessing a
    non-clashing number.
    """

    REWRITE = 1  # mutate the call/args (must run before anything that judges it)
    GATE = 2  # allow/deny the (possibly rewritten) call — the final say


#: Default stage for a control subscriber that does not declare one. Gate is the
#: safe default: an undeclared subscriber judges *after* any declared rewriter.
DEFAULT_STAGE = ControlStage.GATE


@runtime_checkable
class ControlOutcome(Protocol):
    """The typed influence a control subscriber folds back onto the host.

    Each control event has its own concrete outcome (see
    ``common/events/outcomes.py``); the bus drives them all generically through
    just these three members, so a new event's outcome plugs in without touching
    the bus:

    * ``is_blocking`` — the outcome short-circuits the rest of the bucket.
    * ``merge`` — fold two outcomes *of the same event* into one.
    * ``rebind`` — thread a rewrite forward so the next subscriber sees it,
      stamped with ``by`` = the rewriting subscriber's name (provenance).
    """

    @property
    def is_blocking(self) -> bool:
        """True when this outcome halts the action and short-circuits the bucket."""
        ...

    def merge(self, other: "ControlOutcome") -> "ControlOutcome":
        """Fold ``other`` (same event type) into ``self``, returning the result."""
        ...

    def rebind(self, event: Any, *, by: str = "") -> Any:
        """Return ``event`` with this outcome's rewrite threaded in (or unchanged).

        ``by`` is the name of the subscriber that produced this outcome, stamped by
        the bus at the single pairing point so the rewrite records *who* changed the
        event as provenance. Non-rewriting outcomes ignore it and return ``event``.
        """
        ...


@runtime_checkable
class ControlSubscriber(Protocol):
    """Phase-1 subscriber: may fold a :class:`ControlOutcome` to influence the host.

    Declares:

    * ``handles`` — the event *names* (``event.name`` discriminators) it consumes.
      The bus routes each event only to the subscribers that handle it, so a
      subscriber is never even invoked for events it ignores.
    * ``stage`` — its :class:`ControlStage` within a shared bucket. Read via
      ``getattr(sub, "stage", DEFAULT_STAGE)``; single-subscriber buckets need not
      declare it.

    ``name`` is a stable label the bus stamps onto any rewrite this subscriber
    produces (rewrite provenance — *who* changed the event). Read via
    ``getattr(sub, "name", type(sub).__name__)``, so a subscriber that never
    rewrites need not declare it. The bus stamps it at the single pairing point
    (where it knows both the subscriber and the outcome it just produced), so
    attribution cannot be forgotten by a subscriber.

    ``fail_mode`` is read via ``getattr(sub, "fail_mode", FAIL_OPEN)``; only
    security-critical gates opt into ``FAIL_CLOSED``. A fail-closed subscriber
    MUST also expose ``on_failure(reason) -> ControlOutcome`` so the bus can
    synthesize the correct typed deny when the subscriber itself crashes/times
    out (the bus is generic and cannot know which outcome type to build).
    """

    handles: tuple[str, ...]
    stage: ControlStage

    async def handle_control(self, event) -> "Optional[ControlOutcome]":
        """Handle a control event; return a typed influence or ``None``."""
        ...


@runtime_checkable
class ObservationSubscriber(Protocol):
    """Phase-2 fan-out sink: consumes events, never influences the host.

    ``delivery`` is read via ``getattr(sub, "delivery", MIRROR)`` so existing
    sinks need not declare it; only the durable recorder opts into ``DURABLE``.
    """

    priority: int

    async def handle(self, event) -> None:
        """Consume one event. The return value is structurally ignored."""
        ...


__all__ = [
    "DeliveryPolicy",
    "MIRROR",
    "DURABLE",
    "FailMode",
    "FAIL_OPEN",
    "FAIL_CLOSED",
    "ControlStage",
    "DEFAULT_STAGE",
    "ControlOutcome",
    "ControlSubscriber",
    "ObservationSubscriber",
]
