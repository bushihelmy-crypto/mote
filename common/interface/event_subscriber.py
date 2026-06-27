"""Subscriber protocols — the two planes the :class:`EventBus` fans out to.

The bus dispatches every event in **two phases**, and which phase a subscriber
runs in is a structural property of the subscriber, not a flag on the event:

* :class:`ControlSubscriber` (phase 1) — awaited inline, in priority order; its
  ``handle_control`` may return a :class:`HookOutcome` which the bus *folds* and
  hands back to the emitter (veto / mutate args / inject context / stop). There
  is normally exactly one (the hook subscriber). Because only this plane folds,
  influence over the host is confined to subscribers registered here.

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

Leaf module: imports only ``typing`` plus (under TYPE_CHECKING) ``HookOutcome``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from metagpt.common.hook.types import HookOutcome

#: How the observation plane delivers an event to a subscriber.
DeliveryPolicy = Literal["mirror", "durable"]
MIRROR: DeliveryPolicy = "mirror"
DURABLE: DeliveryPolicy = "durable"


@runtime_checkable
class ControlSubscriber(Protocol):
    """Phase-1 subscriber: may fold a :class:`HookOutcome` to influence the host."""

    priority: int

    async def handle_control(self, event) -> "Optional[HookOutcome]":
        """Handle a control event; return a folded influence or ``None``."""
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
    "ControlSubscriber",
    "ObservationSubscriber",
]
