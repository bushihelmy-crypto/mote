"""Bridge a :class:`~mote.common.resilience.CircuitBreaker` onto the event bus.

The breaker primitive is bus-agnostic (a leaf that knows nothing of events); it
emits state changes through an injected ``on_transition`` callback. This module
is the single glue seam that turns such a callback into a
:class:`BreakerStateChangeEvent` observation on the active bus.

Installed once at app start via
``get_health_registry().set_transition_hook(breaker_bus_hook)`` so every breaker
the registry creates thereafter mirrors its transitions onto the bus — observation
only, never influencing the breaker's own verdicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import observe_event_sync
from .types import BreakerStateChangeEvent

if TYPE_CHECKING:
    from mote.common.resilience import BreakerState


def breaker_bus_hook(key: str, old: "BreakerState", new: "BreakerState", reason: str) -> None:
    """A :data:`~mote.common.resilience.TransitionHook` that emits onto the bus.

    Fire-and-forget on the active bus; a no-op when no bus is bound (so a breaker
    tripping outside a runtime scope never raises). ``old``/``new`` are
    ``BreakerState`` (a ``str`` enum) — ``.value`` gives the wire string.
    """
    observe_event_sync(
        BreakerStateChangeEvent(
            key=key,
            old_state=old.value,
            new_state=new.value,
            reason=reason,
        )
    )


__all__ = ["breaker_bus_hook"]
