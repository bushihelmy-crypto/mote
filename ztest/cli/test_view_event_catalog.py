from __future__ import annotations

import inspect

import pytest

from mote.product.interfaces.acp.wire import AcpWireState, to_acp_updates
from mote.product.interfaces.agui.wire import AguiWireState, to_agui_events
from mote.product.presentation.events import events as ev
from mote.product.presentation.events.catalog import (
    VIEW_EVENT_CATALOG,
    VIEW_EVENT_GENERATION,
    VIEW_EVENT_TYPES,
    UnsupportedViewEventError,
    require_view_event,
)


class UnknownViewEvent(ev.ViewEvent):
    kind = "unknown_view_event"


class FakeNotice(ev.ViewEvent):
    kind = ev.NOTICE


def test_catalog_is_closed_over_every_concrete_view_event_type() -> None:
    concrete = {
        value
        for value in vars(ev).values()
        if inspect.isclass(value) and issubclass(value, ev.ViewEvent) and value is not ev.ViewEvent
    }
    assert set(VIEW_EVENT_TYPES) == concrete
    assert len({item.kind for item in VIEW_EVENT_CATALOG}) == len(VIEW_EVENT_CATALOG)
    assert all(item.generation == VIEW_EVENT_GENERATION for item in VIEW_EVENT_CATALOG)


def test_unknown_or_kind_type_mismatch_fails_closed() -> None:
    with pytest.raises(UnsupportedViewEventError):
        require_view_event(UnknownViewEvent())
    with pytest.raises(UnsupportedViewEventError):
        require_view_event(FakeNotice())


def test_wire_adapters_reject_unknown_generation_member() -> None:
    unknown = UnknownViewEvent()
    with pytest.raises(UnsupportedViewEventError):
        to_acp_updates(unknown, AcpWireState("session"))
    with pytest.raises(UnsupportedViewEventError):
        to_agui_events(unknown, AguiWireState("thread", "run"))
