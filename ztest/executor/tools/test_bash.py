#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Bash tool (``mote.executor.tools.bash``).

Drives the REAL subprocess path (aexecute) in the per-test workspace. Covers
stdout capture, stderr capture, exit-code annotation, the stable-cwd model
(a ``cd`` does NOT persist — Codex-aligned), per-call ``workdir`` scoping, and
the empty-command guard.
"""
from __future__ import annotations

import os

import pytest

from mote.executor.tool_result import ToolError
from mote.executor.tools.bash import Bash

from .conftest import CapRole, bind, run


def _bash(tool, **kwargs):
    """Run the tool and return its text output (Bash returns a ToolResult)."""
    return run(tool.call(**kwargs)).output


def _bash_result(tool, **kwargs):
    """Run the tool and return the whole ToolResult (for inspecting ``data``)."""
    return run(tool.call(**kwargs))


def _ready(workspace):
    """Bind a Bash tool to a role whose cwd starts at the workspace."""
    role = CapRole(cwd=str(workspace))
    return bind(Bash(), role), role


class TestBashRun:
    def test_echo_stdout(self, workspace):
        tool, _ = _ready(workspace)
        out = _bash(tool, command="echo hello")
        assert out == "hello"

    def test_stderr_captured(self, workspace):
        tool, _ = _ready(workspace)
        out = _bash(tool, command="echo oops 1>&2")
        assert "oops" in out

    def test_nonzero_exit_annotated(self, workspace):
        tool, _ = _ready(workspace)
        # A subshell exit leaves $? non-zero without killing the wrapper shell,
        # so the trailing probe still runs and reports the code.
        out = _bash(tool, command="(exit 3)")
        # A failed shell command does NOT raise; the rc is annotated instead.
        assert "[exit code: 3]" in out

    def test_zero_exit_no_annotation(self, workspace):
        tool, _ = _ready(workspace)
        out = _bash(tool, command="true")
        assert "exit code" not in out

    def test_empty_output(self, workspace):
        tool, _ = _ready(workspace)
        out = _bash(tool, command=":")
        assert out == ""


class TestBashCwd:
    def test_runs_in_role_cwd(self, workspace):
        sub = workspace / "work"
        sub.mkdir()
        role = CapRole(cwd=str(sub))
        tool = bind(Bash(), role)
        out = _bash(tool, command="pwd")
        assert os.path.realpath(out) == os.path.realpath(str(sub))

    def test_cd_does_not_persist(self, workspace):
        sub = workspace / "deep"
        sub.mkdir()
        tool, role = _ready(workspace)
        _bash(tool, command="cd deep")
        # A `cd` inside the command does NOT drift the session's cwd (Codex model).
        assert os.path.realpath(role.get_cwd()) == os.path.realpath(str(workspace))
        # A follow-up command still runs from the stable base dir, not `deep`.
        out = _bash(tool, command="pwd")
        assert os.path.realpath(out) == os.path.realpath(str(workspace))

    def test_invalid_cwd_falls_back(self, workspace):
        role = CapRole(cwd=str(workspace / "does-not-exist"))
        tool = bind(Bash(), role)
        # Non-existent cwd => aexecute falls back to the process default; no crash.
        out = _bash(tool, command="echo ok")
        assert out == "ok"


class TestBashGuards:
    def test_empty_command_raises(self, workspace):
        tool, _ = _ready(workspace)
        with pytest.raises(ToolError, match="'command' argument is required"):
            _bash(tool, command="   ")

    def test_timeout_returns_message(self, workspace):
        tool, _ = _ready(workspace)
        # Timeout no longer raises: the command is terminated and a "timed out"
        # message is returned (in milliseconds, codex-style).
        out = _bash(tool, command="sleep 5", timeout=0.2)
        assert "timed out after 200 milliseconds" in out

    def test_timeout_returns_partial_output(self, workspace):
        tool, _ = _ready(workspace)
        # Output produced before the timeout is preserved.
        out = _bash(tool, command="echo early; sleep 5", timeout=0.5)
        assert "timed out" in out
        assert "early" in out


class TestBashInputs:
    def test_inputs_object_exported_as_json_env(self, workspace):
        tool, _ = _ready(workspace)
        # The whole object is reachable as $INPUTS (JSON), even nested values.
        out = _bash(tool, command='echo "$INPUTS"', inputs={"a": 1, "nested": {"b": [2, 3]}})
        assert '"a": 1' in out
        assert '"nested"' in out

    def test_scalar_inputs_exported_as_own_env_vars(self, workspace):
        tool, _ = _ready(workspace)
        out = _bash(
            tool,
            command='echo "$name $count $flag"',
            inputs={"name": "hi", "count": 7, "flag": True},
        )
        # str verbatim, number decimal, bool shell-idiomatic.
        assert out == "hi 7 true"

    def test_non_identifier_and_complex_keys_only_via_inputs(self, workspace):
        tool, _ = _ready(workspace)
        # A non-identifier key and a list value are NOT their own env vars, but
        # both still ride along inside $INPUTS.
        out = _bash(
            tool,
            command='printenv items || echo NO_ITEMS; echo "$INPUTS"',
            inputs={"bad-key": "x", "items": [1, 2]},
        )
        assert "NO_ITEMS" in out
        assert '"items"' in out
        assert '"bad-key"' in out

    def test_stdout_json_parsed_into_data(self, workspace):
        tool, _ = _ready(workspace)
        res = _bash_result(tool, command='echo "{\\"k\\": [1, 2]}"')
        # A caller / graph $ref can index into the structured value.
        assert res.data == {"k": [1, 2]}

    def test_stdout_plaintext_is_data_string(self, workspace):
        tool, _ = _ready(workspace)
        res = _bash_result(tool, command="echo plain text")
        assert res.data == "plain text"

    def test_empty_stdout_data_is_none(self, workspace):
        tool, _ = _ready(workspace)
        res = _bash_result(tool, command=":")
        assert res.data is None

    def test_no_inputs_leaves_env_uninjected(self, workspace):
        tool, _ = _ready(workspace)
        # Without inputs, $INPUTS is not set (parent env inherited unchanged).
        out = _bash(tool, command='echo "[${INPUTS:-unset}]"')
        assert out == "[unset]"


class TestBashCheck:
    def test_check_success_on_zero_exit(self, workspace):
        tool, _ = _ready(workspace)
        # A zero exit under check still succeeds, with the structured data intact.
        res = _bash_result(tool, command="echo 42", check=True)
        assert res.success is True
        assert res.data == 42

    def test_check_fails_on_nonzero_exit(self, workspace):
        tool, _ = _ready(workspace)
        res = _bash_result(tool, command="echo boom 1>&2; exit 3", check=True)
        # A non-zero exit under check fails the call so a graph node can isolate it.
        assert res.success is False
        assert "exit code 3" in res.output
        # The command's own output rides along so the model can see what happened.
        assert "boom" in res.output
        # No trustworthy structured result on a failed command.
        assert res.data is None

    def test_no_check_nonzero_exit_still_succeeds(self, workspace):
        tool, _ = _ready(workspace)
        # Without check, a non-zero exit is annotated but the call still succeeds
        # (grep-no-match / diff-with-changes are legitimate non-zero exits).
        res = _bash_result(tool, command="(exit 1)")
        assert res.success is True
        assert "[exit code: 1]" in res.output

    def test_check_ignores_timeout(self, workspace):
        tool, _ = _ready(workspace)
        # A timeout is not a normal exit code — check does not turn it into a
        # success=False (it already carries its own "timed out" message).
        res = _bash_result(tool, command="sleep 5", timeout=0.2, check=True)
        assert res.success is True
        assert "timed out" in res.output


class TestBashWorkdir:
    def test_runs_in_relative_workdir(self, workspace):
        sub = workspace / "rel"
        sub.mkdir()
        tool, _ = _ready(workspace)
        out = _bash(tool, command="pwd", workdir="rel")
        assert os.path.realpath(out) == os.path.realpath(str(sub))

    def test_runs_in_absolute_workdir(self, workspace):
        sub = workspace / "abs"
        sub.mkdir()
        tool, _ = _ready(workspace)
        out = _bash(tool, command="pwd", workdir=str(sub))
        assert os.path.realpath(out) == os.path.realpath(str(sub))

    def test_workdir_does_not_persist(self, workspace):
        sub = workspace / "transient"
        sub.mkdir()
        tool, role = _ready(workspace)
        _bash(tool, command="pwd", workdir="transient")
        # The transient workdir must NOT change the session's persistent cwd.
        assert os.path.realpath(role.get_cwd()) == os.path.realpath(str(workspace))

    def test_missing_workdir_raises(self, workspace):
        tool, _ = _ready(workspace)
        with pytest.raises(ToolError, match="workdir does not exist"):
            _bash(tool, command="pwd", workdir="nope")

    def test_workdir_scopes_one_call(self, workspace):
        sub = workspace / "scoped"
        sub.mkdir()
        tool, role = _ready(workspace)
        # The scoped call runs in the subdirectory...
        out = _bash(tool, command="pwd", workdir="scoped")
        assert os.path.realpath(out) == os.path.realpath(str(sub))
        # ...but the next call (no workdir) is back in the stable base dir.
        out2 = _bash(tool, command="pwd")
        assert os.path.realpath(out2) == os.path.realpath(str(workspace))
        # And the session's cwd never moved.
        assert os.path.realpath(role.get_cwd()) == os.path.realpath(str(workspace))
