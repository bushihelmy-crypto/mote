#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.executor.loop_guard.subscriber.LoopGuardSubscriber`.

Exercises the PostToolUse control subscriber directly (no bus): it must
* ignore non-PostToolUse events (return ``None``),
* return ``None`` while the detector is below threshold,
* fold a :class:`ToolResultOutcome` with a single ``additional_context`` nudge
  once the injected detector trips, and
* thread the injected closures (``resolve_readonly`` / ``sig_of``) through to the
  detector — the subscriber holds no tool import and no signature policy of its
  own, mirroring how the permission gate stays tool-free.

The two closures are trivial fakes here, so the test asserts the *wiring* (that
the subscriber calls them and passes their results to ``record``) without pulling
in a real tool catalog.
"""
from __future__ import annotations

import pytest

from mote.common.events.outcomes import ToolResultOutcome
from mote.common.events.types import PostToolUseEvent, PreToolUseEvent
from mote.common.text.hashing import content_hash
from mote.executor.loop_guard.detector import ThrashDetector
from mote.executor.loop_guard.subscriber import LoopGuardSubscriber

pytestmark = pytest.mark.asyncio


def _sub(failure_threshold=3, no_progress_threshold=3, readonly=False):
    det = ThrashDetector(failure_threshold=failure_threshold, no_progress_threshold=no_progress_threshold)
    return LoopGuardSubscriber(
        det,
        resolve_readonly=lambda name: readonly,
        sig_of=lambda name, args: f"{name}:{sorted(args.items())}",
    )


def _post(tool="Bash", args=None, success=True, response=""):
    return PostToolUseEvent(
        tool_name=tool,
        tool_input=args or {},
        tool_response=response,
        success=success,
    )


class TestRouting:
    async def test_ignores_non_post_event(self):
        sub = _sub()
        out = await sub.handle_control(PreToolUseEvent(tool_name="Bash", tool_input={}))
        assert out is None

    async def test_quiet_below_threshold(self):
        sub = _sub(failure_threshold=3)
        assert await sub.handle_control(_post(success=False)) is None
        assert await sub.handle_control(_post(success=False)) is None


class TestFailureNudge:
    async def test_trips_and_folds_additional_context(self):
        sub = _sub(failure_threshold=3)
        await sub.handle_control(_post(success=False))
        await sub.handle_control(_post(success=False))
        out = await sub.handle_control(_post(success=False))
        assert isinstance(out, ToolResultOutcome)
        assert len(out.additional_context) == 1
        nudge = out.additional_context[0]
        assert "failed 3 times" in nudge
        assert "AskUserQuestion" in nudge

    async def test_same_args_share_streak_via_sig_closure(self):
        sub = _sub(failure_threshold=2)
        # Same args in different key order collapse to one streak (sig closure).
        assert await sub.handle_control(_post(args={"a": 1, "b": 2}, success=False)) is None
        out = await sub.handle_control(_post(args={"b": 2, "a": 1}, success=False))
        assert out is not None


class TestNoProgressNudge:
    async def test_pure_repeat_trips(self):
        sub = _sub(no_progress_threshold=3, readonly=True)
        await sub.handle_control(_post(tool="Read", success=True, response="body"))
        await sub.handle_control(_post(tool="Read", success=True, response="body"))
        out = await sub.handle_control(_post(tool="Read", success=True, response="body"))
        assert isinstance(out, ToolResultOutcome)
        assert "not making progress" in out.additional_context[0]

    async def test_non_readonly_never_trips(self):
        sub = _sub(no_progress_threshold=3, readonly=False)
        for _ in range(5):
            out = await sub.handle_control(_post(tool="Bash", success=True, response="body"))
            assert out is None

    async def test_fingerprint_uses_content_hash(self):
        # Distinct bodies must not share a no-progress streak.
        sub = _sub(no_progress_threshold=2, readonly=True)
        assert content_hash("one") != content_hash("two")
        await sub.handle_control(_post(tool="Read", success=True, response="one"))
        assert await sub.handle_control(_post(tool="Read", success=True, response="two")) is None
