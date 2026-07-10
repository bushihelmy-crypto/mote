#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end tests for the ``ApplyPatch`` tool.

Exercises the full orchestration: multi-hunk Update located by context, Add,
Delete, Move/rename, the read-before-write mtime guard (unread + modified
since read), ``.ipynb`` rejection, transactional no-partial-write on a failed
chunk, CRLF preservation, and the pure-text path running unbound (no Role).
"""
from __future__ import annotations

import os

import pytest

from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools.apply_patch import ApplyPatch

from .conftest import CapRole, bind, mark_read, run, write_file


def _wrap(body: str) -> str:
    return "*** Begin Patch\n" + body + "\n*** End Patch"


def _bound_tool(role: CapRole) -> ApplyPatch:
    return bind(ApplyPatch(), role)


class TestUpdate:
    def test_multi_hunk_update_by_context(self, workspace, role):
        full = write_file(
            "mod.py",
            "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
        )
        mark_read(role, full)
        tool = _bound_tool(role)
        patch = _wrap(
            "*** Update File: mod.py\n"
            "@@ def a():\n"
            "-    return 1\n"
            "+    return 10\n"
            "@@ def b():\n"
            "-    return 2\n"
            "+    return 20"
        )
        out = run(tool.call(input=patch))
        assert "M mod.py" in out.output
        with open(full) as f:
            assert f.read() == "def a():\n    return 10\n\n\ndef b():\n    return 20\n"

    def test_unread_file_blocked(self, workspace, role):
        write_file("mod.py", "x = 1\n")
        tool = _bound_tool(role)
        patch = _wrap("*** Update File: mod.py\n-x = 1\n+x = 2")
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "has not been read" in str(e.value)

    def test_modified_since_read_blocked(self, workspace, role):
        full = write_file("mod.py", "x = 1\n")
        mark_read(role, full)
        # Mutate on disk after the recorded read -> mtime mismatch.
        import time

        time.sleep(0.01)
        write_file("mod.py", "x = 999\n")
        tool = _bound_tool(role)
        patch = _wrap("*** Update File: mod.py\n-x = 999\n+x = 2")
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "modified since" in str(e.value)


class TestAdd:
    def test_add_new_file(self, workspace, role):
        tool = _bound_tool(role)
        patch = _wrap("*** Add File: sub/new.py\n+hello\n+world")
        out = run(tool.call(input=patch))
        assert "A sub/new.py" in out.output
        full = os.path.abspath("sub/new.py")
        with open(full) as f:
            assert f.read() == "hello\nworld\n"
        # A structured add: empty old, the new content, at the dest path.
        assert len(out.file_changes) == 1
        assert out.file_changes[0].path == full
        assert out.file_changes[0].old == ""
        assert out.file_changes[0].new == "hello\nworld\n"

    def test_add_over_existing_nonempty_rejected(self, workspace, role):
        write_file("exists.py", "content\n")
        tool = _bound_tool(role)
        patch = _wrap("*** Add File: exists.py\n+new")
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "already exists" in str(e.value)


class TestDelete:
    def test_delete_file(self, workspace, role):
        full = write_file("gone.py", "bye\n")
        mark_read(role, full)
        tool = _bound_tool(role)
        patch = _wrap("*** Delete File: gone.py")
        out = run(tool.call(input=patch))
        assert "D gone.py" in out.output
        assert not os.path.exists(full)
        # A structured delete: the pre-delete content as old, empty new.
        assert len(out.file_changes) == 1
        assert out.file_changes[0].path == full
        assert out.file_changes[0].old == "bye\n"
        assert out.file_changes[0].new == ""

    def test_delete_missing_file_rejected(self, workspace, role):
        tool = _bound_tool(role)
        patch = _wrap("*** Delete File: nope.py")
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "does not exist" in str(e.value)


class TestMove:
    def test_update_and_move(self, workspace, role):
        full = write_file("old.py", "a = 1\n")
        mark_read(role, full)
        tool = _bound_tool(role)
        patch = _wrap(
            "*** Update File: old.py\n"
            "*** Move to: new.py\n"
            "-a = 1\n"
            "+a = 2"
        )
        out = run(tool.call(input=patch))
        assert "old.py -> new.py" in out.output
        assert not os.path.exists(full)
        with open(os.path.abspath("new.py")) as f:
            assert f.read() == "a = 2\n"
        # A move+update: the change is stamped at the destination path.
        assert len(out.file_changes) == 1
        assert out.file_changes[0].path == os.path.abspath("new.py")
        assert out.file_changes[0].old == "a = 1\n"
        assert out.file_changes[0].new == "a = 2\n"

    def test_move_dest_exists_rejected(self, workspace, role):
        full = write_file("old.py", "a = 1\n")
        write_file("new.py", "occupied\n")
        mark_read(role, full)
        tool = _bound_tool(role)
        patch = _wrap(
            "*** Update File: old.py\n*** Move to: new.py\n-a = 1\n+a = 2"
        )
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "destination already exists" in str(e.value)


class TestRejections:
    def test_ipynb_rejected(self, workspace, role):
        write_file("nb.ipynb", "{}\n")
        tool = _bound_tool(role)
        patch = _wrap("*** Update File: nb.ipynb\n-{}\n+{}")
        with pytest.raises(ToolError) as e:
            run(tool.call(input=patch))
        assert "notebook" in str(e.value).lower()

    def test_empty_input_rejected(self, workspace, role):
        tool = _bound_tool(role)
        with pytest.raises(ToolError):
            run(tool.call(input="   "))

    def test_malformed_patch_rejected(self, workspace, role):
        tool = _bound_tool(role)
        with pytest.raises(ToolError) as e:
            run(tool.call(input="not a patch at all"))
        assert "parse" in str(e.value).lower()


class TestTransactional:
    def test_failed_chunk_writes_nothing(self, workspace, role):
        good = write_file("good.py", "a = 1\n")
        bad = write_file("bad.py", "b = 1\n")
        mark_read(role, good)
        mark_read(role, bad)
        tool = _bound_tool(role)
        # First hunk would succeed; second references a non-existent context.
        patch = _wrap(
            "*** Update File: good.py\n"
            "-a = 1\n"
            "+a = 2\n"
            "*** Update File: bad.py\n"
            "-this line is not in the file\n"
            "+whatever"
        )
        with pytest.raises(ToolError):
            run(tool.call(input=patch))
        # No partial write: good.py is untouched.
        with open(good) as f:
            assert f.read() == "a = 1\n"
        with open(bad) as f:
            assert f.read() == "b = 1\n"


class TestNewlinePreservation:
    def test_crlf_preserved(self, workspace, role):
        full = write_file("crlf.py", "a = 1\r\nb = 2\r\n", newline="")
        mark_read(role, full)
        tool = _bound_tool(role)
        patch = _wrap("*** Update File: crlf.py\n a = 1\n-b = 2\n+b = 3")
        run(tool.call(input=patch))
        with open(full, "rb") as f:
            data = f.read()
        assert b"\r\n" in data
        assert data == b"a = 1\r\nb = 3\r\n"


class TestUnbound:
    def test_pure_text_path_runs_unbound(self, workspace):
        # No Role: read-before-write guards self-skip, writes still happen.
        write_file("mod.py", "x = 1\n")
        tool = bind(ApplyPatch(), None)
        patch = _wrap("*** Update File: mod.py\n-x = 1\n+x = 2")
        out = run(tool.call(input=patch))
        assert "M mod.py" in out.output
        with open(os.path.abspath("mod.py")) as f:
            assert f.read() == "x = 2\n"
