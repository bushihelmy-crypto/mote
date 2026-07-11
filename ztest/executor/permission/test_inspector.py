#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ToolCallInspector — the clean PreToolUse allow/deny gate seam.

The base captures the whole ``ControlSubscriber`` contract (routing, GATE stage,
fail-closed, outcome translation, ``on_failure``) so a capability/allowlist gate
author writes only :meth:`inspect`. These tests exercise the seam directly and
through a live :class:`EventBus` (proving it lands in the ``PreToolUse`` bucket,
folds deny-wins, and judges the *hook-rewritten* args).
"""
from __future__ import annotations

import asyncio

from mote.common.events import EventBus, PreToolUseEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED, ControlStage
from mote.executor.permission import Inspection, ToolCallInspector


def run(coro):
    return asyncio.run(coro)


class _Allowlist(ToolCallInspector):
    """A minimal capability gate: only names in ``allowed`` may run."""

    name = "allowlist"

    def __init__(self, allowed: set[str]) -> None:
        self._allowed = allowed

    async def inspect(self, tool_name, tool_input, facts) -> Inspection:
        if tool_name in self._allowed:
            return Inspection.allowed()
        return Inspection.denied(f"{tool_name} is not on the allowlist")


class _FactsSpy(ToolCallInspector):
    """Records the (tool_input, facts) it was handed so the seam is observable."""

    def __init__(self) -> None:
        self.seen: list = []

    async def inspect(self, tool_name, tool_input, facts) -> Inspection:
        self.seen.append((dict(tool_input), facts))
        return Inspection.allowed()


class _Boom(ToolCallInspector):
    async def inspect(self, tool_name, tool_input, facts) -> Inspection:
        raise RuntimeError("gate exploded")


class TestVerdict:
    def test_allowed_is_permissive(self):
        v = Inspection.allowed()
        assert v.allow is True and v.reason == ""

    def test_denied_carries_reason(self):
        v = Inspection.denied("nope")
        assert v.allow is False and v.reason == "nope"

    def test_default_is_allow(self):
        assert Inspection().allow is True


class TestProtocolShape:
    def test_routes_only_pre_tool_use(self):
        assert _Allowlist(set()).handles == ("pre_tool_use",)

    def test_runs_at_gate_stage(self):
        # After the hook rewriter (REWRITE), so it judges the final args.
        assert _Allowlist(set()).stage == ControlStage.GATE

    def test_fails_closed_by_default(self):
        assert _Allowlist(set()).fail_mode == FAIL_CLOSED

    def test_exposes_on_failure_typed_deny(self):
        out = _Allowlist(set()).on_failure("timeout")
        assert out.behavior == "deny" and out.system_message == "timeout"


class TestHandleControl:
    def test_allow_folds_inert_allow(self):
        gate = _Allowlist({"Read"})
        out = run(gate.handle_control(PreToolUseEvent(tool_name="Read", tool_input={})))
        assert out.behavior == "allow"
        assert out.is_blocking is False

    def test_deny_folds_recoverable_block(self):
        gate = _Allowlist({"Read"})
        out = run(gate.handle_control(PreToolUseEvent(tool_name="Bash", tool_input={})))
        assert out.behavior == "deny"
        assert out.system_message == "Bash is not on the allowlist"
        # Recoverable: the model can pick another action — not a loop-ending stop.
        assert out.stop is False

    def test_ignores_non_tool_events(self):
        assert run(_Allowlist(set()).handle_control(object())) is None

    def test_resolves_facts_from_current_args(self):
        spy = _FactsSpy()
        event = PreToolUseEvent(
            tool_name="Read",
            tool_input={"path": "/x"},
            resolve_facts=lambda args: {"target": args["path"]},
        )
        run(spy.handle_control(event))
        assert spy.seen == [({"path": "/x"}, {"target": "/x"})]

    def test_facts_none_without_resolver(self):
        spy = _FactsSpy()
        run(spy.handle_control(PreToolUseEvent(tool_name="Read", tool_input={})))
        assert spy.seen == [({}, None)]


class TestOnTheBus:
    def test_subscribe_and_deny_folds_through_emit(self):
        bus = EventBus()
        bus.subscribe(_Allowlist({"Read"}))
        out = run(bus.emit(PreToolUseEvent(tool_name="Bash", tool_input={})))
        assert out is not None and out.behavior == "deny"

    def test_subscribe_and_allow_folds_through_emit(self):
        bus = EventBus()
        bus.subscribe(_Allowlist({"Read"}))
        out = run(bus.emit(PreToolUseEvent(tool_name="Read", tool_input={})))
        assert out.behavior == "allow"

    def test_fail_closed_crash_denies(self):
        # A gate that raises must DENY (fail-closed), via the bus-synthesized
        # on_failure — the call is never waved through on a broken gate.
        bus = EventBus()
        bus.subscribe(_Boom())
        out = run(bus.emit(PreToolUseEvent(tool_name="Read", tool_input={})))
        assert out is not None and out.behavior == "deny"

    def test_gate_sees_hook_rewritten_args(self):
        # A REWRITE-stage subscriber mutates the args; the GATE inspector, running
        # after it, must observe the rewritten args (the bus threads them forward).
        from mote.common.events.outcomes import ToolCallOutcome
        from mote.common.interface.event_subscriber import ControlSubscriber

        class _Rewriter(ControlSubscriber):
            handles = ("pre_tool_use",)
            stage = ControlStage.REWRITE
            name = "rewriter"

            async def handle_control(self, event):
                return ToolCallOutcome(behavior="allow", updated_args={"path": "/rewritten"})

        spy = _FactsSpy()
        bus = EventBus()
        bus.subscribe(_Rewriter())
        bus.subscribe(spy)
        run(bus.emit(PreToolUseEvent(tool_name="Read", tool_input={"path": "/orig"})))
        assert spy.seen == [({"path": "/rewritten"}, None)]
