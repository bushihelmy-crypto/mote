from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.runtime.events.telemetry import TelemetryRuntime


class EventA:
    pass


class EventB:
    pass


class HandlerB:
    async def handle(self, event: EventB) -> None:
        del event


async def reject_mismatched_handler(runtime: TelemetryRuntime) -> None:
    await runtime.subscribe_typed(
        TelemetrySubscriptionSpec(TelemetryIdentity("mote.case.bad"), 1, TelemetryOverflow.DROP_NEWEST),
        EventA,
        HandlerB(),
    )
