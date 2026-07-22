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


class TestWholeFileWrite:
    """Empty old_string = the range is the WHOLE file (the former Write tool).

    Creates a new file or fully overwrites an existing one; overwriting goes
    through the same read-before-write guard as a substring edit.
    """

    def test_creates_new_file(self, workspace):
        p = str(workspace / "new.py")
        out = _edit(Edit(), file_path=p, old_string="", new_string="print('hi')\n")
        assert "Created" in out.output
        assert open(p, encoding="utf-8").read() == "print('hi')\n"
        # A creation carries an empty ``old`` and the new content.
        assert len(out.file_changes) == 1
        assert out.file_changes[0].old == ""
        assert out.file_changes[0].new == "print('hi')\n"

    def test_reports_line_and_byte_counts(self, workspace):
        p = str(workspace / "n.txt")
        out = _edit(Edit(), file_path=p, old_string="", new_string="ab\ncd")
        # 2 lines (the last unterminated line counts), 5 bytes.
        assert "2 lines" in out.output
        assert "5 bytes" in out.output

    def test_creates_missing_parent_dirs(self, workspace):
        p = str(workspace / "a" / "b" / "c.txt")
        _edit(Edit(), file_path=p, old_string="", new_string="x")
        assert os.path.isfile(p)

    def test_creates_empty_file(self, workspace):
        # old_string="" and new_string="" is a legitimate create-empty (the no-op
        # guard applies to substring edits only).
        p = str(workspace / "e.txt")
        out = _edit(Edit(), file_path=p, old_string="", new_string="")
        assert "Created" in out.output
        assert open(p, encoding="utf-8").read() == ""

    def test_overwrites_existing_after_read(self, workspace):
        # A whole-file write over an existing file is now allowed (was refused),
        # gated by read-before-write — mark it read, then it overwrites.
        p = write_file(workspace / "exists.py", "existing\n")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Edit(), role)
        out = _edit(tool, file_path=p, old_string="", new_string="brand new\n")
        assert "Updated" in out.output
        assert open(p, encoding="utf-8").read() == "brand new\n"
        # Carries before/after as a structured change.
        assert out.file_changes[0].old == "existing\n"
        assert out.file_changes[0].new == "brand new\n"

    def test_overwrite_unread_file_blocked(self, workspace):
        # Overwriting an existing file the model has not read this session is
        # refused — the same guard a substring edit enforces.
        p = write_file(workspace / "exists.py", "existing\n")
        tool = bind(Edit(), CapRole())
        with pytest.raises(ToolError, match="has not been read this session"):
            _edit(tool, file_path=p, old_string="", new_string="clobber")

    def test_overwrite_modified_since_read_blocked(self, workspace):
        p = write_file(workspace / "exists.py", "existing\n")
        role = CapRole()
        role.record_file_read(p, os.stat(p).st_mtime_ns - 5_000_000)  # stale => modified
        tool = bind(Edit(), role)
        with pytest.raises(ToolError, match="has been modified since"):
            _edit(tool, file_path=p, old_string="", new_string="clobber")

    def test_unbound_overwrite_skips_guard(self, workspace):
        # No role bound => the read-before-write guard self-skips (isolation/tests).
        p = write_file(workspace / "exists.txt", "old")
        out = _edit(Edit(), file_path=p, old_string="", new_string="new")
        assert "Updated" in out.output
        assert open(p, encoding="utf-8").read() == "new"

    def test_content_too_large_raises(self, workspace):
        from mote.common.const.tools import MAX_CONTENT_SIZE_BYTES

        p = str(workspace / "big.txt")
        oversized = "x" * (MAX_CONTENT_SIZE_BYTES + 1)
        with pytest.raises(ToolError, match="exceeds the maximum"):
            _edit(Edit(), file_path=p, old_string="", new_string=oversized)

    def test_whole_file_result_has_no_structural_summary(self, workspace):
        body = '"""Sample module."""\n\ndef alpha():\n    return 1\n'
        p = str(workspace / "big.py")
        out = _edit(Edit(), file_path=p, old_string="", new_string=body)
        assert "Created" in out.output
        assert "<file-outline" not in out.output
        assert "function alpha()" not in out.output
        assert open(p, encoding="utf-8").read() == body

    def test_ipynb_whole_file_write_allowed(self, workspace):
        # A whole-file write CAN emit a .ipynb (raw notebook JSON) — only
        # substring edits of .ipynb are refused (use the notebook edit tool).
        p = str(workspace / "nb.ipynb")
        out = _edit(Edit(), file_path=p, old_string="", new_string='{"cells": []}')
        assert "Created" in out.output
        assert open(p, encoding="utf-8").read() == '{"cells": []}'

    def test_overwrite_preserves_crlf(self, workspace):
        p = write_file(workspace / "crlf.txt", "a\r\nb\r\n", newline="")
        role = CapRole()
        mark_read(role, p)
        tool = bind(Edit(), role)
        # Content arrives LF-normalized; the tool translates to the file's CRLF.
        _edit(tool, file_path=p, old_string="", new_string="x\ny\n")
        assert open(p, "rb").read() == b"x\r\ny\r\n"

    def test_new_file_uses_lf(self, workspace):
        p = str(workspace / "lf.txt")
        _edit(Edit(), file_path=p, old_string="", new_string="x\ny\n")
        assert open(p, "rb").read() == b"x\ny\n"


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
