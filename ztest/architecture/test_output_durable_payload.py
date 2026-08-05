from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest
from pydantic import BaseModel

from mote.contracts.conversation import AIMessage
from mote.contracts.events.output import FinalOutputCommittedEvent, OutputCandidateReceivedEvent, OutputMigratedEvent
from mote.kernel.output import JsonSchemaOutputDecoder, TypeAdapterOutputDecoder


class _Report(BaseModel):
    count: int
    tags: list[str]


@pytest.mark.parametrize(
    ("event_type", "field_name"),
    (
        (OutputCandidateReceivedEvent, "raw"),
        (OutputMigratedEvent, "value"),
        (FinalOutputCommittedEvent, "value"),
    ),
)
def test_durable_output_value_fields_are_json_typed(event_type: type, field_name: str) -> None:
    annotation = next(field.type for field in fields(event_type) if field.name == field_name)
    assert "Any" not in str(annotation)


@pytest.mark.parametrize(
    "factory",
    (
        lambda value: OutputCandidateReceivedEvent(raw=value),
        lambda value: OutputMigratedEvent(value=value),
        lambda value: FinalOutputCommittedEvent(value=value, message=AIMessage(content="done")),
    ),
)
def test_durable_output_events_reject_non_json_values(factory) -> None:
    with pytest.raises(TypeError, match="not JSON-safe"):
        factory(object())
    with pytest.raises(ValueError, match="non-finite"):
        factory(float("nan"))


def test_durable_output_event_cannot_bypass_frozen_json_projection() -> None:
    event = FinalOutputCommittedEvent(
        value={"items": [1, 2]},
        message=AIMessage(content="done"),
        validator_provenance=[{"validator": "schema"}],
    )

    assert event.value == {"items": (1, 2)}
    with pytest.raises(FrozenInstanceError):
        event.value = object()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.validator_provenance = (object(),)  # type: ignore[misc,assignment]


def test_type_adapter_output_encoder_returns_strict_json_value() -> None:
    encoded = TypeAdapterOutputDecoder(_Report).encode(_Report(count=2, tags=["a", "b"]))
    assert encoded == {"count": 2, "tags": ("a", "b")}
    with pytest.raises(TypeError, match="not JSON-safe"):
        JsonSchemaOutputDecoder({}).encode({"invalid": {1, 2}})
