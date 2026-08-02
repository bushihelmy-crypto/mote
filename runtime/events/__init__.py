"""Runtime event infrastructure. Event contracts live in ``contracts.events``."""

from mote.kernel.telemetry.events import current_span_id, span
from mote.runtime.events.breaker import breaker_telemetry_hook
from mote.runtime.events.context import bind_telemetry, current_telemetry, observe_event, observe_event_sync
from mote.runtime.events.dispatcher import SubscriptionBinding, SubscriptionManifest
from mote.runtime.events.fabric import EventFabric
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.events.log_subscriber import LogSubscriber
from mote.runtime.events.scope import ScopePath, ScopeRef, current_scope, push_scope
from mote.runtime.events.stream import log_llm_stream
from mote.runtime.events.telemetry import (
    AllTelemetryBinding,
    TelemetryHandle,
    TelemetryManifest,
    TelemetryRuntime,
    TelemetryState,
)

__all__ = [
    # fabric + telemetry context
    "EventFabric",
    "SubscriptionBinding",
    "SubscriptionManifest",
    "AllTelemetryBinding",
    "TelemetryHandle",
    "TelemetryManifest",
    "TelemetryRuntime",
    "TelemetryState",
    "bind_telemetry",
    "current_telemetry",
    "observe_event",
    "observe_event_sync",
    # activity scope spine
    "ScopeRef",
    "ScopePath",
    "current_scope",
    "push_scope",
    # llm stream
    "log_llm_stream",
    # trace instrumentation
    "span",
    "current_span_id",
    # subscribers
    "LogSubscriber",
    "LocalEventJournal",
    "breaker_telemetry_hook",
]
