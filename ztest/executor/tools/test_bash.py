#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Bash tool (``metagpt.executor.tools.bash``).

Drives the REAL subprocess path (aexecute) in the per-test workspace. Covers
stdout capture, stderr capture, exit-code annotation, cwd persistence across
calls via the get_cwd/set_cwd capabilities (a ``cd`` survives), the empty-command
guard, and the ``_split_probe`` pure helper in isolation.
"""
from __future__ import annotations

import os

import pytest

from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools.bash import Bash, _CWD_MARKER

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

    def test_cd_persists_via_set_cwd(self, workspace):
        sub = workspace / "deep"
        sub.mkdir()
        tool, role = _ready(workspace)
        _bash(tool, command="cd deep")
        # The probe captured the new directory and wrote it back through set_cwd.
        assert os.path.realpath(role.get_cwd()) == os.path.realpath(str(sub))
        # A follow-up command now runs from the persisted directory.
        out = _bash(tool, command="pwd")
        assert os.path.realpath(out) == os.path.realpath(str(sub))

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

    def test_timeout_raises(self, workspace):
        tool, _ = _ready(workspace)
        with pytest.raises(ToolError, match="timed out"):
            _bash(tool, command="sleep 5", timeout=0.2)


# --- Pure-helper unit tests --------------------------------------------------


class TestSplitProbe:
    def test_splits_output_code_and_cwd(self):
        stdout = f"line1\nline2\n{_CWD_MARKER}0:/home/user\n"
        output, rc, cwd = Bash._split_probe(stdout)
        assert output == "line1\nline2"
        assert rc == 0
        assert cwd == "/home/user"

    def test_nonzero_code_parsed(self):
        stdout = f"oops\n{_CWD_MARKER}7:/tmp\n"
        output, rc, cwd = Bash._split_probe(stdout)
        assert output == "oops"
        assert rc == 7
        assert cwd == "/tmp"

    def test_missing_marker_returns_raw(self):
        output, rc, cwd = Bash._split_probe("raw output, no probe")
        assert output == "raw output, no probe"
        assert rc == 0
        assert cwd == ""

    def test_empty_stdout(self):
        assert Bash._split_probe("") == ("", 0, "")

    def test_bad_code_defaults_zero(self):
        stdout = f"x\n{_CWD_MARKER}notanint:/p\n"
        output, rc, cwd = Bash._split_probe(stdout)
        assert rc == 0
        assert cwd == "/p"
