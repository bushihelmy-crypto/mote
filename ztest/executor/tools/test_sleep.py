#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Sleep tool (``metagpt.executor.tools.sleep``).

Sleep delegates the actual wait to the ``wait_interruptible`` Role capability
(which we fake via CapRole), so these tests are instant and deterministic. The
ArtifactsReporter is offline-safe (callback_url defaults to "") so the report
step is a no-op here. Covers the slept / interrupted return wording.
"""
from __future__ import annotations

from metagpt.executor.tools.sleep import Sleep

from .conftest import CapRole, bind, run


def _sleep(tool, **kwargs):
    return run(tool.call(**kwargs))


class TestSleep:
    def test_slept_full_duration(self, workspace):
        # CapRole default returns (duration, False) => slept the full time.
        role = CapRole()
        tool = bind(Sleep(), role)
        out = _sleep(tool, duration_seconds=2.5)
        assert out == "Slept for 2.5s"

    def test_default_duration_is_ten_minutes(self, workspace):
        # Duration is optional; omitting it sleeps the default 600s (10 min).
        role = CapRole()
        tool = bind(Sleep(), role)
        out = _sleep(tool)
        assert out == "Slept for 600.0s"

    def test_interrupted(self, workspace):
        # Script an early wake: slept 0.4s, interrupted.
        role = CapRole(wait_result=(0.4, True))
        tool = bind(Sleep(), role)
        out = _sleep(tool, duration_seconds=300)
        assert out == "Sleep interrupted after 0.4s"

    def test_returns_reported_slept_seconds(self, workspace):
        # The reported seconds come from wait_interruptible, not the request.
        role = CapRole(wait_result=(10.0, False))
        tool = bind(Sleep(), role)
        out = _sleep(tool, duration_seconds=300)
        assert out == "Slept for 10.0s"
