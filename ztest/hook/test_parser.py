#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for parse_command_output / parse_callback_result (CC/codex contract)."""
from __future__ import annotations

import json

from metagpt.common.hook.parser import parse_callback_result, parse_command_output
from metagpt.common.hook.types import HookOutcome


def test_exit_2_blocks_with_stderr_reason():
    out = parse_command_output(stdout="", stderr="nope, blocked", exit_code=2)
    assert out.behavior == "deny"
    assert out.system_message == "nope, blocked"


def test_exit_0_non_json_is_passthrough():
    out = parse_command_output(stdout="just some text", stderr="", exit_code=0)
    assert out.behavior is None
    assert out.additional_context == []


def test_exit_0_empty_is_passthrough():
    out = parse_command_output(stdout="   ", stderr="", exit_code=0)
    assert out.behavior is None


def test_other_nonzero_is_nonblocking_passthrough():
    out = parse_command_output(stdout="", stderr="boom", exit_code=1)
    assert out.behavior is None
    assert out.stop is False


def test_json_decision_approve_and_block():
    assert parse_command_output(json.dumps({"decision": "approve"}), "", 0).behavior == "allow"
    assert parse_command_output(json.dumps({"decision": "block"}), "", 0).behavior == "deny"


def test_json_permission_decision_overrides_decision():
    payload = {
        "decision": "approve",
        "hookSpecificOutput": {"permissionDecision": "deny"},
    }
    assert parse_command_output(json.dumps(payload), "", 0).behavior == "deny"


def test_json_permission_decision_ask():
    payload = {"hookSpecificOutput": {"permissionDecision": "ask"}}
    assert parse_command_output(json.dumps(payload), "", 0).behavior == "ask"


def test_json_updated_input_maps_to_updated_args():
    payload = {"updatedInput": {"command": "ls -la"}}
    out = parse_command_output(json.dumps(payload), "", 0)
    assert out.updated_args == {"command": "ls -la"}


def test_json_additional_context_string_and_list():
    s = parse_command_output(json.dumps({"additionalContext": "ctx"}), "", 0)
    assert s.additional_context == ["ctx"]
    lst = parse_command_output(json.dumps({"additionalContext": ["a", "b"]}), "", 0)
    assert lst.additional_context == ["a", "b"]


def test_json_hook_specific_additional_context():
    payload = {"hookSpecificOutput": {"additionalContext": "extra"}}
    assert parse_command_output(json.dumps(payload), "", 0).additional_context == ["extra"]


def test_json_continue_false_sets_stop():
    payload = {"continue": False, "stopReason": "user halted"}
    out = parse_command_output(json.dumps(payload), "", 0)
    assert out.stop is True
    assert out.stop_reason == "user halted"


def test_json_system_message():
    out = parse_command_output(json.dumps({"systemMessage": "hello"}), "", 0)
    assert out.system_message == "hello"


def test_block_reason_surfaces_as_system_message():
    payload = {"decision": "block", "reason": "bad command"}
    out = parse_command_output(json.dumps(payload), "", 0)
    assert out.behavior == "deny"
    assert out.system_message == "bad command"


def test_callback_none_is_empty():
    out = parse_callback_result(None)
    assert out.behavior is None


def test_callback_dict():
    out = parse_callback_result({"decision": "block"})
    assert out.behavior == "deny"


def test_callback_outcome_returned_as_is():
    o = HookOutcome(behavior="ask")
    assert parse_callback_result(o) is o


def test_callback_unknown_type_ignored():
    out = parse_callback_result(42)
    assert out.behavior is None
