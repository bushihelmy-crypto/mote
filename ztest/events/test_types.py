#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the three observation events added to unify the parallel paths:

``RecoveryEvent`` / ``TaskProgressEvent`` / ``ResourceReportEvent`` — their
discriminators, default fields, and re-export from ``mote.common.events``.
"""
from __future__ import annotations

import mote.common.events as ev
from mote.common.events.types import (
    RECOVERY,
    RESOURCE_REPORT,
    TASK_PROGRESS,
    RecoveryEvent,
    ResourceReportEvent,
    TaskProgressEvent,
)


def test_recovery_event_classvars_and_fields():
    assert RecoveryEvent.name == RECOVERY == "recovery"
    # Events carry no control/observation marker; the plane lives on the
    # subscriber. There must be no ``is_control`` attribute to drift out of sync.
    assert not hasattr(RecoveryEvent, "is_control")
    e = RecoveryEvent(phase="recovered", action="retry", attempt=2, error_type="ValueError", error="boom")
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
    e = TaskProgressEvent(task_id="bg_1", stage="split", status="running", detail="x")
    assert (e.task_id, e.stage, e.status, e.detail) == ("bg_1", "split", "running", "x")
    assert TaskProgressEvent().detail == ""


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
