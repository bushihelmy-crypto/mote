#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Edit tool (``mote.executor.tools.edit``).

Covers exact string replacement, replace_all, the forgiving match cascade (curly
quotes + tab/space), create-via-empty-old_string, the read-before-edit guard,
and the not-found / multi-match / no-change error paths. Also exercises the pure
helper functions directly.
"""
from __future__ import annotations

import os

import pytest
from mote.executor.tool_result import ToolError
from mote.executor.tools.edit import (
    Edit,
    _apply_edit,
    _find_actual_string,
    _normalize_quotes,
    _normalize_whitespace,
    _preserve_quote_style,
)

from .conftest import CapRole, bind, mark_read, run, write_file


def _edit(tool: Edit, **kwargs):
    return run(tool.call(**kwargs))


def _ready(workspace, name, content):
    """Write a file, bind an Edit tool to a role that has 'read' it. Returns (tool, path, role)."""
    p = write_file(workspace / name, content)
    role = CapRole()
    mark_read(role, p)
    return bind(Edit(), role), p, role


class TestExactReplace:
    def test_replaces_single_occurrence(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "x = 1\ny = 2\n")
        out = _edit(tool, file_path=p, old_string="x = 1", new_string="x = 42")
        assert "has been updated successfully" in out.output
        assert open(p, encoding="utf-8").read() == "x = 42\ny = 2\n"

    def test_update_carries_structured_file_change(self, workspace):
        # The edit result carries old/new full contents as a structured fact so
        # the view layer renders the change without sniffing the output text.
        tool, p, _ = _ready(workspace, "a.py", "x = 1\ny = 2\n")
        out = _edit(tool, file_path=p, old_string="x = 1", new_string="x = 42")
        assert len(out.file_changes) == 1
        ch = out.file_changes[0]
        assert ch.path == p
        assert ch.old == "x = 1\ny = 2\n"
        assert ch.new == "x = 42\ny = 2\n"

    def test_multiple_matches_without_replace_all_raises(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "a\na\n")
        with pytest.raises(ToolError, match="found 2 matches"):
            _edit(tool, file_path=p, old_string="a", new_string="b")

    def test_replace_all(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "a\na\na\n")
        out = _edit(tool, file_path=p, old_string="a", new_string="b", replace_all=True)
        assert "All 3 occurrences were" in out.output
        assert open(p, encoding="utf-8").read() == "b\nb\nb\n"

    def test_string_not_found_raises(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "hello\n")
        with pytest.raises(ToolError, match="string to replace not found"):
            _edit(tool, file_path=p, old_string="goodbye", new_string="x")

    def test_identical_old_new_raises(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "hello\n")
        with pytest.raises(ToolError, match="no changes to make"):
            _edit(tool, file_path=p, old_string="hello", new_string="hello")

    def test_delete_consumes_trailing_newline(self, workspace):
        tool, p, _ = _ready(workspace, "a.py", "keep\nremove\nkeep2\n")
        _edit(tool, file_path=p, old_string="remove", new_string="")
        # The line is removed entirely, no blank line left behind.
        assert open(p, encoding="utf-8").read() == "keep\nkeep2\n"


class TestCreateViaEmptyOld:
    def test_creates_new_file(self, workspace):
        p = str(workspace / "new.py")
        out = _edit(Edit(), file_path=p, old_string="", new_string="print('hi')\n")
        assert "has been created successfully" in out.output
        assert open(p, encoding="utf-8").read() == "print('hi')\n"
        # A creation carries an empty ``old`` and the new content.
        assert len(out.file_changes) == 1
        assert out.file_changes[0].old == ""
        assert out.file_changes[0].new == "print('hi')\n"

    def test_refuses_to_clobber_nonempty(self, workspace):
        p = write_file(workspace / "exists.py", "existing\n")
        with pytest.raises(ToolError, match="already exists with content"):
            _edit(Edit(), file_path=p, old_string="", new_string="new")

    def test_fills_existing_empty_file(self, workspace):
        p = write_file(workspace / "empty.py", "   \n")
        out = _edit(Edit(), file_path=p, old_string="", new_string="filled")
        assert "has been updated successfully" in out.output
        assert open(p, encoding="utf-8").read() == "filled"


class TestEditGuards:
    def test_edit_unread_file_blocked(self, workspace):
        p = write_file(workspace / "a.py", "x\n")
        tool = bind(Edit(), CapRole())  # role bound but file not "read"
        with pytest.raises(ToolError, match="has not been read this session"):
            _edit(tool, file_path=p, old_string="x", new_string="y")

    def test_missing_file_nonempty_old_raises(self, workspace):
        tool = bind(Edit(), CapRole())
        with pytest.raises(ToolError, match="file does not exist"):
            _edit(tool, file_path=str(workspace / "nope.py"), old_string="x", new_string="y")

    def test_ipynb_refused(self, workspace):
        p = write_file(workspace / "nb.ipynb", "{}")
        with pytest.raises(ToolError, match="Jupyter notebook"):
            _edit(Edit(), file_path=p, old_string="{}", new_string="[]")

    def test_directory_refused(self, workspace):
        with pytest.raises(ToolError, match="is a directory"):
            _edit(Edit(), file_path=str(workspace), old_string="x", new_string="y")


class TestMatchCascade:
    def test_tab_space_normalized_match(self, workspace):
        # File uses a real tab; the model copies it as 4 spaces (Read renders so).
        tool, p, _ = _ready(workspace, "t.py", "def f():\n\treturn 1\n")
        out = _edit(tool, file_path=p, old_string="    return 1", new_string="    return 2")
        assert "updated successfully" in out.output
        assert "return 2" in open(p, encoding="utf-8").read()

    def test_curly_quote_normalized_match(self, workspace):
        tool, p, _ = _ready(workspace, "q.py", "s = \u201chello\u201d\n")
        # Model emits straight quotes; matcher normalizes curly->straight.
        out = _edit(tool, file_path=p, old_string='s = "hello"', new_string='s = "world"')
        assert "updated successfully" in out.output


class TestEditNewlinePreservation:
    def test_crlf_preserved(self, workspace):
        p = write_file(workspace / "crlf.py", "a\r\nb\r\n", newline="")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Edit(), role)
        _edit(tool, file_path=p, old_string="a", new_string="x")
        assert open(p, "rb").read() == b"x\r\nb\r\n"


# --- Pure-helper unit tests --------------------------------------------------


class TestHelpers:
    def test_normalize_quotes(self):
        assert _normalize_quotes("\u2018a\u2019 \u201cb\u201d") == "'a' \"b\""

    def test_normalize_whitespace_expands_tabs(self):
        assert _normalize_whitespace("\tx") == "    x"

    def test_find_actual_string_exact(self):
        assert _find_actual_string("abc def", "def") == "def"

    def test_find_actual_string_missing(self):
        assert _find_actual_string("abc", "zzz") is None

    def test_find_actual_string_via_quotes(self):
        # Curly in file, straight in search => matched substring is the curly form.
        assert _find_actual_string("x = \u201chi\u201d", '"hi"') == "\u201chi\u201d"

    def test_apply_edit_replace_all(self):
        assert _apply_edit("a a a", "a", "b", True) == "b b b"

    def test_apply_edit_single(self):
        assert _apply_edit("a a a", "a", "b", False) == "b a a"

    def test_apply_edit_delete_consumes_newline(self):
        assert _apply_edit("keep\ndrop\n", "drop", "", False) == "keep\n"

    def test_preserve_quote_style_no_op_on_exact_match(self):
        # When old_string matched exactly, new_string is untouched.
        assert _preserve_quote_style('"x"', '"x"', '"y"') == '"y"'

    def test_preserve_quote_style_restyles_double(self):
        # Matched via curly normalization => new_string's double quotes go curly.
        result = _preserve_quote_style('"x"', "\u201cx\u201d", 'say "hi"')
        assert "\u201c" in result or "\u201d" in result


class TestCwdResolution:
    def test_create_relative_resolves_against_role_cwd(self, workspace, tmp_path):
        # An empty old_string creates the file; a bound Edit resolves the relative
        # path against the ROLE's stable cwd, not the process cwd.
        sub = tmp_path / "role_dir"
        sub.mkdir()
        role = CapRole(cwd=str(sub))
        _edit(bind(Edit(), role), file_path="fresh.txt", old_string="", new_string="hi\n")
        assert os.path.isfile(sub / "fresh.txt")
        assert not os.path.isfile(workspace / "fresh.txt")

    def test_create_relative_unbound_uses_process_cwd(self, workspace):
        _edit(Edit(), file_path="unbound.txt", old_string="", new_string="ok\n")
        assert os.path.isfile(workspace / "unbound.txt")
