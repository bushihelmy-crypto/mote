#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the patch grammar parser (``_apply_patch.parser.parse_patch``).

Covers the codex marker grammar: Add / Delete / Update / Move hunks, ``@@``
context anchors (named + empty), the ``*** End of File`` marker, lenient heredoc
unwrapping, and the explicit ``ApplyPatchError`` diagnostics on malformed input.
"""
from __future__ import annotations

import pytest

from mote.executor.dependency._apply_patch import (
    AddFile,
    ApplyPatchError,
    DeleteFile,
    UpdateFile,
    affected_paths,
    hunk_path,
    parse_patch,
)


def _wrap(body: str) -> str:
    return "*** Begin Patch\n" + body + "\n*** End Patch"


class TestAddFile:
    def test_add_file_collects_plus_lines(self):
        patch = _wrap("*** Add File: foo/new.py\n+line one\n+line two")
        hunks = parse_patch(patch)
        assert len(hunks) == 1
        add = hunks[0]
        assert isinstance(add, AddFile)
        assert add.path == "foo/new.py"
        assert add.contents == "line one\nline two\n"

    def test_add_empty_file(self):
        patch = _wrap("*** Add File: empty.py")
        hunks = parse_patch(patch)
        assert isinstance(hunks[0], AddFile)
        assert hunks[0].contents == ""


class TestDeleteFile:
    def test_delete_file(self):
        patch = _wrap("*** Delete File: gone.py")
        hunks = parse_patch(patch)
        assert len(hunks) == 1
        assert isinstance(hunks[0], DeleteFile)
        assert hunks[0].path == "gone.py"


class TestUpdateFile:
    def test_update_with_context_and_change(self):
        patch = _wrap("*** Update File: edit.py\n" "@@ def f():\n" " a = 1\n" "-    b = 2\n" "+    b = 3")
        hunks = parse_patch(patch)
        assert len(hunks) == 1
        upd = hunks[0]
        assert isinstance(upd, UpdateFile)
        assert upd.path == "edit.py"
        assert upd.move_path is None
        assert len(upd.chunks) == 1
        chunk = upd.chunks[0]
        assert chunk.change_context == "def f():"
        assert chunk.old_lines == ["a = 1", "    b = 2"]
        assert chunk.new_lines == ["a = 1", "    b = 3"]

    def test_empty_context_marker(self):
        patch = _wrap("*** Update File: edit.py\n@@\n a\n-b\n+c")
        upd = parse_patch(patch)[0]
        assert upd.chunks[0].change_context is None

    def test_end_of_file_marker(self):
        patch = _wrap("*** Update File: edit.py\n" " last context\n" "+appended\n" "*** End of File")
        upd = parse_patch(patch)[0]
        assert upd.chunks[0].is_end_of_file is True

    def test_multiple_chunks(self):
        patch = _wrap("*** Update File: edit.py\n" "@@ first\n" " a\n" "-b\n" "+B\n" "@@ second\n" " x\n" "-y\n" "+Y\n")
        upd = parse_patch(patch)[0]
        assert len(upd.chunks) == 2
        assert upd.chunks[0].change_context == "first"
        assert upd.chunks[1].change_context == "second"


class TestMove:
    def test_update_with_move(self):
        patch = _wrap("*** Update File: old.py\n" "*** Move to: new.py\n" "@@ ctx\n" " a\n" "-b\n" "+c\n")
        upd = parse_patch(patch)[0]
        assert isinstance(upd, UpdateFile)
        assert upd.path == "old.py"
        assert upd.move_path == "new.py"
        assert hunk_path(upd) == "new.py"


class TestMultiHunk:
    def test_add_delete_update_in_one_patch(self):
        patch = _wrap(
            "*** Add File: a.py\n"
            "+x\n"
            "*** Delete File: b.py\n"
            "*** Update File: c.py\n"
            "@@ ctx\n"
            " keep\n"
            "-drop\n"
            "+add\n"
        )
        hunks = parse_patch(patch)
        assert [type(h).__name__ for h in hunks] == [
            "AddFile",
            "DeleteFile",
            "UpdateFile",
        ]
        assert affected_paths(hunks) == [
            ("a.py", "add"),
            ("b.py", "delete"),
            ("c.py", "update"),
        ]

    def test_affected_paths_uses_move_destination(self):
        patch = _wrap("*** Update File: old.py\n" "*** Move to: new.py\n" "@@ ctx\n" " a\n" "-b\n" "+c\n")
        hunks = parse_patch(patch)
        assert affected_paths(hunks) == [("new.py", "move")]


class TestLenientHeredoc:
    def test_heredoc_wrapper_stripped(self):
        patch = "<<EOF\n" "*** Begin Patch\n" "*** Add File: foo.py\n" "+hi\n" "*** End Patch\n" "EOF"
        hunks = parse_patch(patch)
        assert len(hunks) == 1
        assert isinstance(hunks[0], AddFile)
        assert hunks[0].path == "foo.py"

    def test_quoted_heredoc_wrapper_stripped(self):
        patch = "<<'EOF'\n" "*** Begin Patch\n" "*** Delete File: gone.py\n" "*** End Patch\n" "EOF"
        hunks = parse_patch(patch)
        assert isinstance(hunks[0], DeleteFile)


class TestMalformed:
    def test_missing_begin_marker_raises(self):
        with pytest.raises(ApplyPatchError):
            parse_patch("*** Add File: foo.py\n+x\n*** End Patch")

    def test_missing_end_marker_raises(self):
        with pytest.raises(ApplyPatchError):
            parse_patch("*** Begin Patch\n*** Add File: foo.py\n+x")

    def test_invalid_hunk_header_raises(self):
        with pytest.raises(ApplyPatchError):
            parse_patch(_wrap("*** Frobnicate File: foo.py"))

    def test_empty_patch_raises(self):
        with pytest.raises(ApplyPatchError):
            parse_patch("")

    def test_bad_update_line_raises(self):
        # A line inside an Update hunk that doesn't start with a recognised sigil
        # and follows existing chunk content.
        patch = _wrap("*** Update File: edit.py\n a\n-b\n+c\nnonsense")
        with pytest.raises(ApplyPatchError):
            parse_patch(patch)
