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
