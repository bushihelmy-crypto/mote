"""Persistence policy stays orthogonal to fact-stream consumers."""

from __future__ import annotations

from mote.runtime.events import (
    OutputCommittedEvent,
    PromptRejectedEvent,
    TaskProgressEvent,
    ToolInvocationStartedEvent,
    TurnEndEvent,
)
from mote.runtime.session.event_policy import ROLLOUT_EVENT_TYPES, is_rollout_event


def test_rollout_policy_selects_facts_without_changing_their_types():
    assert is_rollout_event(OutputCommittedEvent())
    assert is_rollout_event(
        PromptRejectedEvent(
            prompt_digest="sha256:deadbeef",
            redacted_excerpt="denied",
            classification="deny",
            reason="denied",
        )
    )
    assert is_rollout_event(TurnEndEvent())
    assert not hasattr(TurnEndEvent, "outcome_type")
    assert not hasattr(OutputCommittedEvent, "outcome_type")


def test_non_persisted_events_remain_available_to_other_observers():
    assert not is_rollout_event(TaskProgressEvent())
    assert not is_rollout_event(ToolInvocationStartedEvent())


def test_rollout_policy_has_no_duplicate_or_instance_order_semantics():
    assert isinstance(ROLLOUT_EVENT_TYPES, frozenset)
    assert len(ROLLOUT_EVENT_TYPES) == 15
