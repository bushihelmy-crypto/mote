#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Write tool (``mote.executor.tools.write``).

Covers file creation (with parent dirs), the read-before-overwrite guard backed
by the Role's shared file-read state, newline-style preservation, and the size /
type guards.
"""
from __future__ import annotations

import os

import pytest

from mote.executor.tool_result import ToolError
from mote.executor.tools.write import Write

from .conftest import CapRole, bind, mark_read, run, write_file


def _write(tool: Write, **kwargs):
    return run(tool.call(**kwargs))


class TestCreate:
    def test_creates_new_file(self, workspace):
        p = str(workspace / "new.txt")
        out = _write(Write(), file_path=p, content="hello\nworld\n")
        assert "Created" in out
        assert open(p, encoding="utf-8").read() == "hello\nworld\n"

    def test_creates_missing_parent_dirs(self, workspace):
        p = str(workspace / "a" / "b" / "c.txt")
        _write(Write(), file_path=p, content="x")
        assert os.path.isfile(p)

    def test_empty_content_creates_empty_file(self, workspace):
        p = str(workspace / "e.txt")
        out = _write(Write(), file_path=p, content="")
        assert "Created" in out
        assert open(p, encoding="utf-8").read() == ""

    def test_line_and_byte_counts_reported(self, workspace):
        p = str(workspace / "n.txt")
        out = _write(Write(), file_path=p, content="ab\ncd")
        # 2 lines (no trailing newline counts the last line), 5 bytes.
        assert "2 lines" in out
        assert "5 bytes" in out

    def test_empty_path_raises(self, workspace):
        with pytest.raises(ToolError, match="'file_path' argument is required"):
            _write(Write(), file_path="  ", content="x")

    def test_directory_target_raises(self, workspace):
        with pytest.raises(ToolError, match="is a directory"):
            _write(Write(), file_path=str(workspace), content="x")


class TestOverwriteGuard:
    def test_overwrite_unread_file_blocked_when_bound(self, workspace):
        p = write_file(workspace / "exists.txt", "old")
        role = CapRole()
        tool = bind(Write(), role)
        with pytest.raises(ToolError, match="has not been read this session"):
            _write(tool, file_path=p, content="new")

    def test_overwrite_after_read_allowed(self, workspace):
        p = write_file(workspace / "exists.txt", "old")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Write(), role)
        out = _write(tool, file_path=p, content="new")
        assert "Updated" in out
        assert open(p, encoding="utf-8").read() == "new"

    def test_overwrite_modified_since_read_blocked(self, workspace):
        p = write_file(workspace / "exists.txt", "old")
        role = CapRole()
        # Record a stale mtime so the file looks modified-since-read.
        role.record_file_read(p, os.stat(p).st_mtime_ns - 5_000_000)
        tool = bind(Write(), role)
        with pytest.raises(ToolError, match="has been modified since"):
            _write(tool, file_path=p, content="new")

    def test_unbound_overwrite_skips_guard(self, workspace):
        p = write_file(workspace / "exists.txt", "old")
        # No role bound => guard self-skips, overwrite proceeds.
        out = _write(Write(), file_path=p, content="new")
        assert "Updated" in out

    def test_refreshes_read_state_after_write(self, workspace):
        p = write_file(workspace / "exists.txt", "old")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Write(), role)
        _write(tool, file_path=p, content="new")
        # Read-state now matches the just-written file => a follow-up write is allowed.
        assert role.get_file_read_mtime(p) == os.stat(p).st_mtime_ns


class TestNewlinePreservation:
    def test_crlf_preserved_on_overwrite(self, workspace):
        p = write_file(workspace / "crlf.txt", "a\r\nb\r\n", newline="")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Write(), role)
        # Content arrives LF-normalized; the tool translates to the file's CRLF.
        _write(tool, file_path=p, content="x\ny\n")
        raw = open(p, "rb").read()
        assert b"\r\n" in raw
        assert raw == b"x\r\ny\r\n"

    def test_new_file_uses_lf(self, workspace):
        p = str(workspace / "lf.txt")
        _write(Write(), file_path=p, content="x\ny\n")
        assert open(p, "rb").read() == b"x\ny\n"


class TestCwdResolution:
    def test_relative_path_resolves_against_role_cwd(self, workspace, tmp_path):
        # A bound Write resolves a relative path against the ROLE's stable cwd,
        # not the process cwd (the workspace fixture chdir'd into `workspace`).
        sub = tmp_path / "role_dir"
        sub.mkdir()
        role = CapRole(cwd=str(sub))
        _write(bind(Write(), role), file_path="made.txt", content="hi\n")
        assert os.path.isfile(sub / "made.txt")
        assert not os.path.isfile(workspace / "made.txt")

    def test_relative_path_unbound_uses_process_cwd(self, workspace):
        # Unbound: relative paths fall back to the process cwd.
        _write(Write(), file_path="unbound.txt", content="ok\n")
        assert os.path.isfile(workspace / "unbound.txt")
