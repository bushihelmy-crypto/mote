from typing import TypeGuard

from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.runtime.events.telemetry import TypedTelemetryBinding


class EventA:
    pass


class EventB:
    pass


def is_event_a(event: object) -> TypeGuard[EventA]:
    return isinstance(event, EventA)


class HandlerB:
    async def handle(self, event: EventB) -> None:
        del event


bad = TypedTelemetryBinding(
    TelemetrySubscriptionSpec(TelemetryIdentity("mote.case.bad"), 1, TelemetryOverflow.DROP_NEWEST),
    is_event_a,
    HandlerB(),
)
