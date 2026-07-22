#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.executor.loop_guard.detector.ThrashDetector`.

The detector is a pure per-Role state machine (zero I/O, zero framework imports),
so it is exercised in complete isolation here — no bus, no event, no executor.
Covers the two orthogonal streaks and their self-healing reset semantics:

* repeated-failure: same ``(tool, sig)`` failing N times in a row trips once at
  the threshold; any success clears the count; a different signature never
  contributes to a peer's streak.
* no-progress: a PURE tool returning the identical fingerprint N times in a row
  trips; a changed fingerprint resets to 1; a non-PURE (LOCAL/EXTERNAL) tool is
  never eligible even when its result is byte-identical.
* cross-shape exclusivity: a failure voids a live no-progress streak and vice
  versa, so one call is only ever evidence for one shape.
"""
from __future__ import annotations

import pytest

from mote.executor.loop_guard.detector import ThrashDetector, Verdict

SIG_A = '[{"args": {"path": "a"}, "name": "Read"}]'
SIG_B = '[{"args": {"path": "b"}, "name": "Read"}]'


def _fail(det: ThrashDetector, tool="Bash", sig=SIG_A):
    return det.record(tool_name=tool, sig=sig, success=False, is_readonly=False, result_fingerprint="")


def _ok_read(det: ThrashDetector, fp, tool="Read", sig=SIG_A, readonly=True):
    return det.record(tool_name=tool, sig=sig, success=True, is_readonly=readonly, result_fingerprint=fp)


class TestRepeatedFailure:
    def test_below_threshold_is_quiet(self):
        det = ThrashDetector(failure_threshold=3)
        assert _fail(det) is None
        assert _fail(det) is None

    def test_trips_exactly_at_threshold(self):
        det = ThrashDetector(failure_threshold=3)
        _fail(det)
        _fail(det)
        v = _fail(det)
        assert v == Verdict(kind="repeated_failure", tool_name="Bash", count=3)

    def test_keeps_tripping_past_threshold_with_rising_count(self):
        det = ThrashDetector(failure_threshold=3)
        _fail(det)
        _fail(det)
        assert _fail(det).count == 3
        assert _fail(det).count == 4

    def test_success_clears_failure_streak(self):
        det = ThrashDetector(failure_threshold=3)
        _fail(det)
        _fail(det)
        # A success on the SAME signature heals the streak.
        det.record(tool_name="Bash", sig=SIG_A, success=True, is_readonly=False, result_fingerprint="x")
        assert _fail(det) is None  # streak restarted at 1
        assert _fail(det) is None  # 2

    def test_different_signature_has_own_streak(self):
        det = ThrashDetector(failure_threshold=3)
        _fail(det, sig=SIG_A)
        _fail(det, sig=SIG_A)
        assert _fail(det, sig=SIG_B) is None  # B is a fresh streak, count 1
        assert _fail(det, sig=SIG_A).count == 3  # A trips independently

    def test_threshold_floor_of_one(self):
        det = ThrashDetector(failure_threshold=0)  # clamped to 1
        assert _fail(det).count == 1


class TestBoundedState:
    def test_evicts_coldest_key_past_cap(self):
        # Cap of 2 distinct signatures; a third distinct failing sig evicts the
        # least-recently-touched one, so its stale count-of-1 is forgotten.
        det = ThrashDetector(failure_threshold=3, max_tracked_keys=2)
        _fail(det, sig="sig-1")
        _fail(det, sig="sig-2")
        _fail(det, sig="sig-3")  # evicts sig-1 (coldest)
        # sig-1's streak was dropped: two more failures restart from 1, not resume.
        assert _fail(det, sig="sig-1") is None  # count 1 (fresh)
        assert _fail(det, sig="sig-1") is None  # count 2

    def test_rewrite_keeps_key_hot(self):
        # Re-touching sig-1 before adding sig-3 makes sig-2 the coldest instead.
        det = ThrashDetector(failure_threshold=3, max_tracked_keys=2)
        _fail(det, sig="sig-1")  # {1}
        _fail(det, sig="sig-2")  # {1,2}
        _fail(det, sig="sig-1")  # sig-1 now hottest at count 2: {2,1}
        _fail(det, sig="sig-3")  # evicts sig-2
        # sig-1 survived with its streak intact — one more failure trips at 3.
        assert _fail(det, sig="sig-1").count == 3

    def test_max_keys_floor_of_one(self):
        det = ThrashDetector(failure_threshold=3, max_tracked_keys=0)  # clamped to 1
        _fail(det, sig="sig-1")
        _fail(det, sig="sig-2")  # evicts sig-1
        assert _fail(det, sig="sig-1") is None  # fresh count 1


class TestNoProgress:
    def test_identical_read_trips_at_threshold(self):
        det = ThrashDetector(no_progress_threshold=3)
        assert _ok_read(det, "same") is None
        assert _ok_read(det, "same") is None
        v = _ok_read(det, "same")
        assert v == Verdict(kind="no_progress", tool_name="Read", count=3)

    def test_changed_result_resets_streak(self):
        det = ThrashDetector(no_progress_threshold=3)
        _ok_read(det, "same")
        _ok_read(det, "same")
        assert _ok_read(det, "different") is None  # reset to 1
        assert _ok_read(det, "different") is None  # 2

    def test_non_readonly_never_eligible(self):
        det = ThrashDetector(no_progress_threshold=3)
        for _ in range(5):
            assert _ok_read(det, "same", tool="Bash", readonly=False) is None

    def test_different_sig_own_no_progress_streak(self):
        det = ThrashDetector(no_progress_threshold=3)
        _ok_read(det, "same", sig=SIG_A)
        _ok_read(det, "same", sig=SIG_A)
        assert _ok_read(det, "same", sig=SIG_B) is None  # B fresh
        assert _ok_read(det, "same", sig=SIG_A).count == 3


class TestCrossShapeExclusivity:
    def test_failure_voids_live_no_progress(self):
        det = ThrashDetector(failure_threshold=3, no_progress_threshold=3)
        # Two identical PURE reads build a no-progress streak of 2.
        det.record(tool_name="Read", sig=SIG_A, success=True, is_readonly=True, result_fingerprint="same")
        det.record(tool_name="Read", sig=SIG_A, success=True, is_readonly=True, result_fingerprint="same")
        # A failure on the same signature voids no-progress, starts a failure streak.
        det.record(tool_name="Read", sig=SIG_A, success=False, is_readonly=True, result_fingerprint="")
        # Resuming identical reads restarts no-progress at 1, so no trip on the next.
        assert (
            det.record(tool_name="Read", sig=SIG_A, success=True, is_readonly=True, result_fingerprint="same") is None
        )

    def test_success_voids_live_failure(self):
        det = ThrashDetector(failure_threshold=3, no_progress_threshold=3)
        det.record(tool_name="Read", sig=SIG_A, success=False, is_readonly=True, result_fingerprint="")
        det.record(tool_name="Read", sig=SIG_A, success=False, is_readonly=True, result_fingerprint="")
        # A PURE success clears the 2-failure streak AND opens no-progress at 1.
        assert _ok_read(det, "same") is None
        # One more failure now restarts the failure streak at 1 (not 3).
        assert det.record(tool_name="Read", sig=SIG_A, success=False, is_readonly=True, result_fingerprint="") is None
