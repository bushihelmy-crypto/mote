"""Persistence policy stays orthogonal to fact-stream consumers."""

from __future__ import annotations

from mote.contracts.conversation import AIMessage
from mote.contracts.events.conversation import PromptRejectedEvent
from mote.contracts.events.output import FinalOutputCommittedEvent
from mote.contracts.events.session import TurnEndEvent
from mote.contracts.events.task import TaskProgressEvent
from mote.contracts.events.tool import ToolInvocationStartedEvent
from mote.contracts.task.progress import ProgressPhase
from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.runtime.session.event_policy import ROLLOUT_EVENT_TYPES, is_rollout_event


def test_rollout_policy_selects_facts_without_changing_their_types():
    assert is_rollout_event(FinalOutputCommittedEvent(message=AIMessage(content="done")))
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
    assert not hasattr(FinalOutputCommittedEvent, "outcome_type")


def test_non_persisted_events_remain_available_to_other_observers():
    assert not is_rollout_event(
        TaskProgressEvent.activity(
            run_id="run",
            definition_id="definition",
            stage="node",
            phase=ProgressPhase.RUNNING,
        )
    )
    assert not is_rollout_event(
        ToolInvocationStartedEvent(
            ToolInvocationIdentity(
                ToolInvocationId("test-call"),
                ToolAttemptOrdinal(1),
                "definition",
                1,
                "sha256-args",
                "owner",
                "run",
            )
        )
    )


def test_rollout_policy_has_no_duplicate_or_instance_order_semantics():
    assert isinstance(ROLLOUT_EVENT_TYPES, frozenset)
    assert len(ROLLOUT_EVENT_TYPES) == 12
