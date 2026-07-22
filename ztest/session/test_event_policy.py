"""Persistence policy stays orthogonal to EventBus planes and consumers."""
from __future__ import annotations

from mote.common.events import OutputCommittedEvent, PreToolUseEvent, TaskProgressEvent, TurnEndEvent
from mote.common.events.types import ControlEvent
from mote.session.event_policy import ROLLOUT_EVENT_TYPES, is_rollout_event


def test_rollout_policy_does_not_define_the_event_plane():
    assert is_rollout_event(OutputCommittedEvent())
    assert is_rollout_event(TurnEndEvent())
    assert issubclass(TurnEndEvent, ControlEvent)
    assert not issubclass(OutputCommittedEvent, ControlEvent)


def test_non_persisted_events_remain_available_to_other_observers():
    assert not is_rollout_event(TaskProgressEvent())
    assert not is_rollout_event(PreToolUseEvent())


def test_rollout_policy_has_no_duplicate_or_instance_order_semantics():
    assert isinstance(ROLLOUT_EVENT_TYPES, frozenset)
    assert len(ROLLOUT_EVENT_TYPES) == 13
