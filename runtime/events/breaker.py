"""Bridge a :class:`~mote.runtime.resilience.CircuitBreaker` onto telemetry.

The breaker primitive is telemetry-agnostic (a leaf that knows nothing of events); it
emits state changes through an injected ``on_transition`` callback. This module
is the single glue seam that turns such a callback into a
:class:`BreakerStateChangeEvent` observation on active telemetry.

Installed once at app start via
``context.health_registry.set_transition_hook(breaker_telemetry_hook)`` so every breaker
the registry creates thereafter mirrors its transitions onto telemetry — observation
only, never influencing the breaker's own verdicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.contracts.events.model import BreakerStateChangeEvent

from .context import observe_event_sync

if TYPE_CHECKING:
    from mote.runtime.resilience import BreakerState


def breaker_telemetry_hook(key: str, old: "BreakerState", new: "BreakerState", reason: str) -> None:
    """Emit one breaker transition onto active telemetry.

    Fire-and-forget; a no-op when no telemetry is bound (so a breaker
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


__all__ = ["breaker_telemetry_hook"]
