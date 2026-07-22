#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Sleep tool (``mote.executor.tools.sleep``).

Sleep delegates the actual wait to the ``wait_interruptible`` Role capability
(which we fake via CapRole), so these tests are instant and deterministic. The
ArtifactsReporter is offline-safe (callback_url defaults to "") so the report
step is a no-op here. Covers the slept / interrupted return wording.
"""
from __future__ import annotations

from mote.executor.tools.sleep import _MAX_WAIT_SECONDS, Sleep

from .conftest import CapRole, bind, run


def _sleep(tool, **kwargs):
    return run(tool.call(**kwargs))


class TestSleep:
    def test_woke_default(self, workspace):
        # CapRole default returns 0.0 => woke immediately (event-driven).
        role = CapRole()
        tool = bind(Sleep(), role)
        out = _sleep(tool)
        assert out == "Woke after 0.0s"

    def test_returns_reported_slept_seconds(self, workspace):
        # The reported seconds come from wait_interruptible.
        role = CapRole(wait_result=10.0)
        tool = bind(Sleep(), role)
        out = _sleep(tool)
        assert out == "Woke after 10.0s"

    def test_caps_wait_at_ceiling(self, workspace):
        # Sleep takes no args now — it always passes the safety ceiling to
        # wait_interruptible so a stuck agent can never park forever.
        role = CapRole(wait_result=3.0)
        tool = bind(Sleep(), role)
        out = _sleep(tool)
        assert out == "Woke after 3.0s"
        assert role.wait_durations == [_MAX_WAIT_SECONDS]
