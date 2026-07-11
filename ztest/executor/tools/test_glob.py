#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Glob tool (``mote.executor.tools.glob``).

Covers pattern matching (recursive **), the mtime ordering, the 100-file
truncation note, VCS-dir exclusion, and the directory-validation guards. ripgrep
is available here, so ``call`` exercises the real binary path; the pure-Python
fallback engine is tested directly so it stays covered everywhere.
"""
from __future__ import annotations

import os

import pytest
from mote.executor.tool_result import ToolError
from mote.executor.tools.glob import Glob

from .conftest import CapRole, bind, run, write_file


def _glob(**kwargs):
    return run(Glob().call(**kwargs))


@pytest.fixture
def tree(workspace):
    write_file(workspace / "a.py", "x")
    write_file(workspace / "b.py", "x")
    write_file(workspace / "c.txt", "x")
    sub = workspace / "sub"
    sub.mkdir()
    write_file(sub / "d.py", "x")
    vcs = workspace / ".git"
    vcs.mkdir()
    write_file(vcs / "config.py", "x")  # must be excluded
    return workspace


class TestGlobMatch:
    def test_recursive_py(self, tree):
        out = _glob(pattern="**/*.py")
        assert "a.py" in out
        assert os.path.join("sub", "d.py") in out
        assert "c.txt" not in out

    def test_basename_pattern_matches_any_depth(self, tree):
        # A separator-less pattern matches by basename at any depth (rg semantics).
        out = _glob(pattern="*.py")
        assert "a.py" in out
        assert os.path.join("sub", "d.py") in out

    def test_excludes_vcs_dir(self, tree):
        out = _glob(pattern="**/*.py")
        assert ".git" not in out

    def test_no_matches(self, tree):
        out = _glob(pattern="**/*.rs")
        assert out == "No files found"

    def test_path_argument_scopes_search(self, tree):
        out = _glob(pattern="*.py", path=str(tree / "sub"))
        assert "d.py" in out
        assert "a.py" not in out


class TestGlobGuards:
    def test_empty_pattern_raises(self, workspace):
        with pytest.raises(ToolError, match="'pattern' argument is required"):
            _glob(pattern="  ")

    def test_missing_dir_raises(self, workspace):
        with pytest.raises(ToolError, match="directory does not exist"):
            _glob(pattern="*.py", path=str(workspace / "nope"))

    def test_path_not_a_directory_raises(self, workspace):
        f = write_file(workspace / "f.txt", "x")
        with pytest.raises(ToolError, match="not a directory"):
            _glob(pattern="*.py", path=f)


class TestGlobFormat:
    def test_sorted_by_mtime_recent_first(self, workspace):
        old = write_file(workspace / "old.py", "x")
        new = write_file(workspace / "new.py", "x")
        # Make old.py distinctly older.
        st = os.stat(old)
        os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000))
        out = _glob(pattern="*.py")
        lines = [ln for ln in out.splitlines() if ln.endswith(".py")]
        assert lines.index("new.py") < lines.index("old.py")

    def test_truncation_note(self, workspace):
        for i in range(120):
            write_file(workspace / f"f{i}.py", "x")
        out = _glob(pattern="*.py")
        assert "truncated" in out
        files = [ln for ln in out.splitlines() if ln.endswith(".py")]
        assert len(files) == 100

    def test_empty_format(self):
        assert Glob._format([], os.getcwd()) == "No files found"


class TestGlobCwdResolution:
    def test_default_root_is_role_cwd(self, tmp_path, workspace):
        # A bound Glob with no `path` searches the ROLE's stable cwd, not the
        # process cwd (the workspace fixture chdir'd into `workspace`).
        sub = tmp_path / "role_dir"
        sub.mkdir()
        write_file(sub / "role_only.py", "x")
        write_file(workspace / "process_only.py", "x")
        role = CapRole(cwd=str(sub))
        out = run(bind(Glob(), role).call(pattern="*.py"))
        assert "role_only.py" in out
        assert "process_only.py" not in out

    def test_relative_path_resolves_against_role_cwd(self, tmp_path, workspace):
        sub = tmp_path / "role_dir"
        nested = sub / "nested"
        nested.mkdir(parents=True)
        write_file(nested / "deep.py", "x")
        role = CapRole(cwd=str(sub))
        out = run(bind(Glob(), role).call(pattern="*.py", path="nested"))
        assert "deep.py" in out


class TestGlobPythonFallback:
    def test_run_python_recursive(self, tree):
        files = Glob._run_python(str(tree), "**/*.py")
        bases = {os.path.basename(f) for f in files}
        assert {"a.py", "b.py", "d.py"} <= bases
        assert "c.txt" not in {os.path.basename(f) for f in files}

    def test_run_python_excludes_vcs(self, tree):
        files = Glob._run_python(str(tree), "*.py")
        assert all(".git" not in f for f in files)


class TestGlobGlimpse:
    """P2: Glob records matched .py files as code-map glimpse hints."""

    def test_records_matched_py_files(self, tree):
        role = CapRole()
        tool = bind(Glob(), role)
        run(tool.call(pattern="**/*.py"))
        # a.py, b.py, sub/d.py matched (.git excluded); all recorded as abspaths.
        assert any(p.endswith("a.py") for p in role.glimpsed)
        assert any(p.endswith("d.py") for p in role.glimpsed)
        assert all(os.path.isabs(p) for p in role.glimpsed)

    def test_non_py_matches_not_recorded(self, tree):
        role = CapRole()
        tool = bind(Glob(), role)
        run(tool.call(pattern="*.txt"))
        assert role.glimpsed == []  # c.txt has no structure to map

    def test_unbound_glob_does_not_raise(self, tree):
        # No Role bound -> record_file_glimpsed absent -> the glimpse pass no-ops.
        out = _glob(pattern="**/*.py")
        assert "a.py" in out
