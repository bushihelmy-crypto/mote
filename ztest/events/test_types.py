#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the three observation events added to unify the parallel paths:

``RecoveryEvent`` / ``TaskProgressEvent`` / ``ResourceReportEvent`` — their
discriminators, default fields, and re-export from ``mote.runtime.events``.
"""

from __future__ import annotations

import mote.runtime.events as ev
from mote.contracts.events.conversation import PROMPT_REJECTED, PromptRejectedEvent
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


def test_reexported_from_events_package():
    assert ev.RecoveryEvent is RecoveryEvent
    assert ev.TaskProgressEvent is TaskProgressEvent
    assert ev.ResourceReportEvent is ResourceReportEvent
    for n in ("RECOVERY", "TASK_PROGRESS", "RESOURCE_REPORT"):
        assert n in ev.__all__
    for n in ("RecoveryEvent", "TaskProgressEvent", "ResourceReportEvent"):
        assert n in ev.__all__


def test_prompt_rejected_is_a_distinct_safe_observation_fact():
    event = PromptRejectedEvent(
        prompt_digest="sha256:deadbeef",
        redacted_excerpt="safe prompt",
        classification="deny",
        reason="denied",
        terminate=True,
    )

    assert event.name == PROMPT_REJECTED == "prompt_rejected"
    assert ev.PromptRejectedEvent is PromptRejectedEvent
    assert "PROMPT_REJECTED" in ev.__all__
    assert "PromptRejectedEvent" in ev.__all__
