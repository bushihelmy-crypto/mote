from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from mote.contracts.events.family_policy import (
    EVENT_FAMILY_POLICIES,
    ConsumerEffectPolicy,
    EventDeliveryGuarantee,
    EventFamily,
)
from mote.contracts.ports.events.telemetry import TelemetryEmitter
from mote.kernel.telemetry.events import KernelTelemetryEvent, emit_event

ROOT = Path(__file__).resolve().parents[2]


def test_event_family_matrix_has_distinct_correctness_guarantees() -> None:
    assert set(EVENT_FAMILY_POLICIES) == set(EventFamily)
    assert EVENT_FAMILY_POLICIES[EventFamily.CONTROL].delivery is EventDeliveryGuarantee.TYPED_RECEIPT
    assert (
        EVENT_FAMILY_POLICIES[EventFamily.OBSERVATION].consumer_effect is ConsumerEffectPolicy.NO_AUTHORITATIVE_MUTATION
    )
    assert EVENT_FAMILY_POLICIES[EventFamily.OBSERVATION].delivery is EventDeliveryGuarantee.BOUNDED_LOSS


def test_emitter_protocol_preserves_its_event_type_parameter() -> None:
    source = (ROOT / "contracts/ports/events/telemetry.py").read_text(encoding="utf-8")
    assert "class TelemetryEmitter(Protocol[EventT_contra])" in source
    assert "emit(self, event: EventT_contra)" in source
    assert "class EventNarrower" not in source


def test_kernel_observer_accepts_only_kernel_observation_union() -> None:
    assert get_type_hints(emit_event)["event"] == KernelTelemetryEvent
    source = (ROOT / "kernel/telemetry/events.py").read_text(encoding="utf-8")
    assert "Any" not in source
    assert "runtime" not in source


def test_type_erasure_is_private_to_runtime_owner() -> None:
    source = (ROOT / "runtime/events/telemetry.py").read_text(encoding="utf-8")
    assert "TypeGuard" not in source
    assert "class _EventNarrower" not in source
    assert "class _TypedTelemetryBinding" in source
    assert "event_type: type[EventT]" in source
    assert "def erase(self) -> _ErasedTelemetryBinding" in source
