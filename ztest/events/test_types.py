#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the three observation events added to unify the parallel paths:

``RecoveryEvent`` / ``TaskProgressEvent`` / ``ResourceReportEvent`` — their
discriminators and default fields. Event types are imported from their
authoritative Contracts modules, never re-exported by Runtime.
"""

from __future__ import annotations

import pytest

from mote.contracts.events.conversation import PROMPT_REJECTED, PromptRejectedEvent
from mote.contracts.events.file.facts import FileTransactionPreparedEvent
from mote.contracts.events.model import RoutingDecisionEvent
from mote.contracts.events.output import OutputCandidateReceivedEvent
from mote.contracts.events.task import TASK_PROGRESS, TaskProgressEvent
from mote.contracts.events.telemetry import RECOVERY, RESOURCE_REPORT, RecoveryEvent, ResourceReportEvent
from mote.contracts.task.progress import ProgressPhase


def test_recovery_event_classvars_and_fields():
    assert RecoveryEvent.name == RECOVERY == "recovery"
    # Events are facts; control belongs to domain Policies.
    assert not hasattr(RecoveryEvent, "is_control")
    e = RecoveryEvent(
        phase="recovered",
        action="retry",
        attempt=2,
        error_type="ValueError",
        error="boom",
    )
    assert (e.phase, e.action, e.attempt, e.error_type, e.error) == (
        "recovered",
        "retry",
        2,
        "ValueError",
        "boom",
    )
    # defaults
    d = RecoveryEvent()
    assert d.phase == "recovered" and d.action == "" and d.attempt == 0


def test_task_progress_event_classvars_and_fields():
    assert TaskProgressEvent.name == TASK_PROGRESS == "task_progress"
    assert not hasattr(TaskProgressEvent, "is_control")
    e = TaskProgressEvent.activity(
        run_id="run-1",
        definition_id="definition-1",
        stage="split",
        phase=ProgressPhase.RUNNING,
        detail="x",
    )
    assert e.progress.identity.execution_id == "run-1"
    assert (e.stage, e.status, e.detail) == ("split", "running", "x")


def test_resource_report_event_uses_name_underscore():
    assert ResourceReportEvent.name == RESOURCE_REPORT == "resource_report"
    assert not hasattr(ResourceReportEvent, "is_control")
    # ``name_`` carries the report's data-type name; ``name`` stays the
    # ClassVar discriminator (they must not collide).
    e = ResourceReportEvent(block="Terminal", name_="path", value="/x", uuid="u", role="r")
    assert e.name == "resource_report"
    assert e.name_ == "path"
    assert (e.block, e.value, e.uuid, e.role) == ("Terminal", "/x", "u", "r")
    assert ResourceReportEvent().extra is None


def test_prompt_rejected_is_a_distinct_safe_observation_fact():
    event = PromptRejectedEvent(
        prompt_digest="sha256:deadbeef",
        redacted_excerpt="safe prompt",
        classification="deny",
        reason="denied",
        terminate=True,
    )

    assert event.name == PROMPT_REJECTED == "prompt_rejected"


@pytest.mark.parametrize(
    ("decoder", "canonical", "wrong_primitive_field"),
    (
        (
            RoutingDecisionEvent.from_payload,
            {"decision": {}, "state": {}, "route_schema_version": 2},
            "route_schema_version",
        ),
        (
            PromptRejectedEvent.from_payload,
            {
                "prompt_digest": "sha256:deadbeef",
                "redacted_excerpt": "safe",
                "classification": "deny",
                "reason": "denied",
                "terminate": False,
            },
            "terminate",
        ),
        (
            OutputCandidateReceivedEvent.from_payload,
            {
                "candidate_id": "candidate-1",
                "contract_id": "contract-1",
                "schema_fingerprint": "sha256:schema",
                "representation": "json",
                "raw": None,
                "run_id": "run-1",
                "run_kind": "agent",
            },
            "run_id",
        ),
        (
            FileTransactionPreparedEvent.from_payload,
            {"mutation_set": {}, "hunks": []},
            "hunks",
        ),
    ),
)
def test_registered_d3_decoders_reject_noncanonical_payloads(
    decoder,
    canonical,
    wrong_primitive_field,
):
    missing = dict(canonical)
    missing.pop(next(iter(missing)))
    with pytest.raises(ValueError):
        decoder(missing)

    extra = dict(canonical)
    extra["unexpected"] = None
    with pytest.raises(ValueError):
        decoder(extra)

    wrong_primitive = dict(canonical)
    original = wrong_primitive[wrong_primitive_field]
    wrong_primitive[wrong_primitive_field] = "wrong" if type(original) is not str else 7
    with pytest.raises((TypeError, ValueError)):
        decoder(wrong_primitive)
