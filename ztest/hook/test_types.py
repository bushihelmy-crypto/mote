#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the pure-data hook types: HookInput wire shape + fold precedence."""
from __future__ import annotations

from metagpt.common.hook.types import EMPTY, HookInput, HookOutcome, fold


def test_to_json_dict_carries_envelope_and_payload():
    hi = HookInput(
        hook_event_name="PreToolUse",
        session_id="sid",
        cwd="/tmp/proj",
        transcript_path="/tmp/rollout.jsonl",
        permission_mode="default",
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    wire = hi.to_json_dict()
    # Both snake_case and camelCase identity keys present (CC/codex compatible).
    assert wire["hook_event_name"] == "PreToolUse"
    assert wire["hookEventName"] == "PreToolUse"
    assert wire["sessionId"] == "sid"
    assert wire["cwd"] == "/tmp/proj"
    assert wire["permissionMode"] == "default"
    # Payload fields merged at top level.
    assert wire["tool_name"] == "Bash"
    assert wire["tool_input"] == {"command": "ls"}


def test_to_json_dict_omits_permission_mode_when_none():
    wire = HookInput(hook_event_name="Stop").to_json_dict()
    assert "permission_mode" not in wire
    assert "permissionMode" not in wire


def test_fold_empty_is_empty():
    out = fold([])
    assert out.behavior is None
    assert out.additional_context == []
    assert out is not EMPTY  # fresh instance


def test_fold_deny_beats_allow_regardless_of_order():
    out = fold([HookOutcome(behavior="allow"), HookOutcome(behavior="deny")])
    assert out.behavior == "deny"
    # allow after deny must not override.
    out2 = fold([HookOutcome(behavior="deny"), HookOutcome(behavior="allow")])
    assert out2.behavior == "deny"


def test_fold_ask_beats_allow_but_loses_to_deny():
    assert fold([HookOutcome(behavior="allow"), HookOutcome(behavior="ask")]).behavior == "ask"
    assert fold([HookOutcome(behavior="ask"), HookOutcome(behavior="deny")]).behavior == "deny"


def test_fold_accumulates_context_and_takes_last_system_message():
    out = fold(
        [
            HookOutcome(additional_context=["a"], system_message="first"),
            HookOutcome(additional_context=["b", "c"], system_message="second"),
        ]
    )
    assert out.additional_context == ["a", "b", "c"]
    assert out.system_message == "second"


def test_fold_stop_is_sticky():
    out = fold([HookOutcome(stop=True, stop_reason="halt"), HookOutcome()])
    assert out.stop is True
    assert out.stop_reason == "halt"


def test_fold_takes_last_updated_args():
    out = fold([HookOutcome(updated_args={"x": 1}), HookOutcome(updated_args={"x": 2})])
    assert out.updated_args == {"x": 2}


def test_is_blocking():
    assert HookOutcome(behavior="deny").is_blocking is True
    assert HookOutcome(stop=True).is_blocking is True
    assert HookOutcome(behavior="allow").is_blocking is False
    assert HookOutcome().is_blocking is False
