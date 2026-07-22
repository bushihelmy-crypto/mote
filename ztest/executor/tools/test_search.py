#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the unified Search tool (``mote.executor.tools.search``).

Search subsumes the legacy Grep + Glob tools via two orthogonal, optional axes:

- ``files``  — a glob selecting WHICH files (the former Glob).
- ``content`` — a regex matching WHAT content (the former Grep).

They compose: files-only lists files, content-only greps, both greps within the
globbed set, and neither is an error. These tests cover all four combinations,
the three content output modes, glob/type filters, pagination, the cwd
resolution, code-map glimpses, the error guards, and the shared engine helpers.
ripgrep is present in this environment, so ``call`` exercises the real binary;
the pure-Python glob fallback and the helper functions are tested directly.
"""
from __future__ import annotations

import os

import pytest

from mote.executor.tool_result import ToolError
from mote.executor.tools._search_engine import apply_head_limit, find_ripgrep, split_glob
from mote.executor.tools.search import Search

from .conftest import CapRole, bind, run, write_file


def _search(**kwargs):
    # Search.call returns a ToolResult; existing assertions target the text.
    return run(Search().call(**kwargs)).output


def _search_result(**kwargs):
    # Full ToolResult, for asserting the structured data={"files": [...]}.
    return run(Search().call(**kwargs))


@pytest.fixture
def tree(workspace):
    """A small file tree to search."""
    write_file(workspace / "a.py", "import os\ndef foo():\n    return ERROR\n")
    write_file(workspace / "b.py", "x = 1\nfoo()\n")
    write_file(workspace / "c.txt", "no match here\nERROR in text\n")
    sub = workspace / "sub"
    sub.mkdir()
    write_file(sub / "d.py", "def bar():\n    pass\n")
    vcs = workspace / ".git"
    vcs.mkdir()
    write_file(vcs / "config.py", "ERROR")  # must be excluded
    return workspace


# --- Axis selection: which of the four paths a call takes ---------------------


class TestAxisSelection:
    def test_neither_axis_raises(self, workspace):
        with pytest.raises(ToolError, match="at least one is required"):
            _search()

    def test_files_only_lists_files(self, tree):
        out = _search(files="**/*.py")
        assert "a.py" in out
        assert os.path.join("sub", "d.py") in out
        assert "c.txt" not in out

    def test_content_only_greps_all_files(self, tree):
        out = _search(content="ERROR")
        # Default files_with_matches mode emits "path:line".
        assert "a.py:3" in out
        assert "c.txt:2" in out
        assert "b.py" not in out

    def test_files_and_content_scopes_grep_to_glob(self, tree):
        # content restricted to *.py -> the c.txt ERROR line is filtered out.
        out = _search(files="*.py", content="ERROR")
        assert "a.py:3" in out
        assert "c.txt" not in out


# --- Structured data: the matched-file list on ToolResult.data ---------------


class TestStructuredData:
    """Every path returns data={"files": [<abs path>, ...]} mirroring the text.

    The list lets a run_graph map fan a per-file edit out over the hits without
    re-parsing the path:line text; it must be deduplicated, absolute, and agree
    with what the human-readable output shows.
    """

    def test_files_axis_lists_absolute_paths(self, tree):
        res = _search_result(files="**/*.py")
        files = res.data["files"]
        assert all(os.path.isabs(p) for p in files)
        bases = {os.path.basename(p) for p in files}
        assert {"a.py", "b.py", "d.py"} <= bases
        assert "c.txt" not in bases

    def test_files_with_matches_lists_matched_files(self, tree):
        res = _search_result(content="ERROR")
        bases = {os.path.basename(p) for p in res.data["files"]}
        assert {"a.py", "c.txt"} == bases  # b.py has no ERROR
        assert all(os.path.isabs(p) for p in res.data["files"])

    def test_content_mode_dedupes_files_with_many_matches(self, workspace):
        # Two ERROR lines in one file -> the file appears once in data.
        write_file(workspace / "many.py", "ERROR one\nok\nERROR two\n")
        res = _search_result(content="ERROR", output_mode="content")
        assert res.output.count("many.py") == 2  # two matching lines shown
        assert res.data["files"] == [os.path.abspath(str(workspace / "many.py"))]

    def test_count_mode_lists_files(self, tree):
        res = _search_result(content="ERROR", output_mode="count")
        bases = {os.path.basename(p) for p in res.data["files"]}
        assert {"a.py", "c.txt"} == bases

    def test_empty_result_gives_empty_file_list(self, tree):
        assert _search_result(files="**/*.rs").data["files"] == []
        assert _search_result(content="NO_SUCH_TOKEN").data["files"] == []

    def test_file_list_respects_pagination(self, tree):
        # head_limit caps the shown files; data mirrors the paginated window.
        res = _search_result(content="ERROR", head_limit=1)
        assert len(res.data["files"]) == 1


# --- File-listing axis (former Glob) -----------------------------------------


class TestFilesAxis:
    def test_basename_pattern_matches_any_depth(self, tree):
        out = _search(files="*.py")
        assert "a.py" in out
        assert os.path.join("sub", "d.py") in out

    def test_excludes_vcs_dir(self, tree):
        out = _search(files="**/*.py")
        assert ".git" not in out

    def test_no_matches(self, tree):
        assert _search(files="**/*.rs") == "No files found"

    def test_path_scopes_search(self, tree):
        out = _search(files="*.py", path=str(tree / "sub"))
        assert "d.py" in out
        assert "a.py" not in out

    def test_sorted_by_mtime_recent_first(self, workspace):
        old = write_file(workspace / "old.py", "x")
        write_file(workspace / "new.py", "x")
        st = os.stat(old)
        os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000))
        out = _search(files="*.py")
        lines = [ln for ln in out.splitlines() if ln.endswith(".py")]
        assert lines.index("new.py") < lines.index("old.py")

    def test_no_file_cap(self, workspace):
        # All matches returned; a large result is persisted to disk by the shared
        # tool-result exit, not truncated here.
        for i in range(120):
            write_file(workspace / f"f{i}.py", "x")
        out = _search(files="*.py")
        assert "truncated" not in out
        assert len([ln for ln in out.splitlines() if ln.endswith(".py")]) == 120

    def test_files_only_on_a_file_raises(self, tree):
        # A pure name search anchored at a single file makes no sense.
        with pytest.raises(ToolError, match="not a directory"):
            _search(files="*.py", path=str(tree / "a.py"))


class TestPyFallback:
    def test_py_files_recursive(self, tree):
        found = Search._py_files(str(tree), "**/*.py")
        bases = {os.path.basename(f) for f in found}
        assert {"a.py", "b.py", "d.py"} <= bases
        assert "c.txt" not in bases

    def test_py_files_excludes_vcs(self, tree):
        found = Search._py_files(str(tree), "*.py")
        assert all(".git" not in f for f in found)

    def test_py_files_empty_pattern_lists_all(self, tree):
        found = Search._py_files(str(tree), "")
        bases = {os.path.basename(f) for f in found}
        assert {"a.py", "c.txt"} <= bases


# --- Content axis (former Grep) ----------------------------------------------


class TestContentMode:
    def test_files_with_matches_lineno(self, tree):
        out = _search(content="ERROR")
        assert "a.py:3" in out

    def test_glob_filter(self, tree):
        out = _search(content="ERROR", files="*.py")
        assert "a.py:3" in out
        assert "c.txt" not in out

    def test_type_filter(self, tree):
        out = _search(content="ERROR", type="py")
        assert "a.py" in out
        assert "c.txt" not in out

    def test_content_output_with_line_numbers(self, tree):
        out = _search(content="ERROR", output_mode="content")
        assert "a.py:3:" in out
        assert "return ERROR" in out

    def test_case_insensitive(self, tree):
        write_file(tree / "e.py", "lowercase error\n")
        out = _search(content="error", output_mode="content", case_insensitive=True)
        assert "e.py" in out
        assert "a.py" in out

    def test_context_lines(self, tree):
        out = _search(content="return ERROR", output_mode="content", context=1)
        assert "def foo" in out

    def test_no_matches(self, tree):
        assert _search(content="zzz-not-present-anywhere") == "No files found"


class TestCountMode:
    def test_per_file_counts_and_summary(self, tree):
        write_file(tree / "many.txt", "ERROR\nERROR\nERROR\n")
        out = _search(content="ERROR", output_mode="count")
        assert "many.txt:3" in out
        assert "Found" in out and "occurrences" in out


class TestPagination:
    def test_head_limit_truncates(self, workspace):
        for i in range(10):
            write_file(workspace / f"f{i}.txt", "ERROR\n")
        out = _search(content="ERROR", head_limit=3)
        body = [ln for ln in out.splitlines() if ln and not ln.startswith("Found")]
        assert len(body) == 3
        assert "limit: 3" in out

    def test_offset_paginates(self, workspace):
        for i in range(5):
            write_file(workspace / f"f{i}.txt", "ERROR\n")
        out = _search(content="ERROR", head_limit=2, offset=2)
        assert "offset: 2" in out


# --- Guards ------------------------------------------------------------------


class TestGuards:
    def test_invalid_output_mode_raises(self, workspace):
        with pytest.raises(ToolError, match="invalid output_mode"):
            _search(content="x", output_mode="bogus")

    def test_missing_path_raises(self, workspace):
        with pytest.raises(ToolError, match="path does not exist"):
            _search(content="x", path=str(workspace / "nope"))


# --- ripgrep hard dependency for content search ------------------------------


class TestRipgrepRequired:
    def test_missing_rg_raises_for_content_search(self, tree, monkeypatch):
        import mote.executor.tools.search as search_mod

        monkeypatch.setattr(search_mod, "find_ripgrep", lambda: None)
        with pytest.raises(ToolError, match="is required for content search"):
            _search(content="ERROR")

    def test_files_axis_works_without_rg(self, tree, monkeypatch):
        # File listing has a pure-Python fallback, so it must not raise when rg
        # is absent.
        import mote.executor.tools.search as search_mod

        monkeypatch.setattr(search_mod, "find_ripgrep", lambda: None)
        out = _search(files="**/*.py")
        assert "a.py" in out

    def test_doc_only_type_works_without_rg(self, workspace, monkeypatch):
        import mote.executor.tools.search as search_mod

        monkeypatch.setattr(search_mod, "find_ripgrep", lambda: None)
        out = _search(content="anything", type="pdf")
        assert out == "No files found"


# --- On-demand document pass gating ------------------------------------------


class TestDocumentGating:
    def test_plain_code_search_skips_documents(self, tree):
        assert Search._targets_documents(str(tree), "", "") is False

    def test_doc_type_triggers(self, tree):
        assert Search._targets_documents(str(tree), "", "pdf") is True

    def test_doc_glob_triggers(self, tree):
        assert Search._targets_documents(str(tree), "*.pdf", "") is True
        assert Search._targets_documents(str(tree), "*.{docx,xlsx}", "") is True

    def test_single_document_file_triggers(self, workspace):
        write_file(workspace / "report.pdf", "unused")
        assert Search._targets_documents(str(workspace / "report.pdf"), "", "") is True

    def test_single_code_file_does_not_trigger(self, tree):
        assert Search._targets_documents(str(tree / "a.py"), "", "") is False


# --- cwd resolution ----------------------------------------------------------


class TestCwdResolution:
    def test_files_default_root_is_role_cwd(self, tmp_path, workspace):
        sub = tmp_path / "role_dir"
        sub.mkdir()
        write_file(sub / "role_only.py", "x")
        write_file(workspace / "process_only.py", "x")
        role = CapRole(cwd=str(sub))
        out = run(bind(Search(), role).call(files="*.py")).output
        assert "role_only.py" in out
        assert "process_only.py" not in out

    def test_content_default_root_is_role_cwd(self, tmp_path, workspace):
        sub = tmp_path / "role_dir"
        sub.mkdir()
        write_file(sub / "role.py", "TARGET here\n")
        write_file(workspace / "process.py", "TARGET here\n")
        role = CapRole(cwd=str(sub))
        out = run(bind(Search(), role).call(content="TARGET")).output
        assert "role.py" in out
        assert "process.py" not in out

    def test_relative_path_resolves_against_role_cwd(self, tmp_path):
        sub = tmp_path / "role_dir"
        nested = sub / "nested"
        nested.mkdir(parents=True)
        write_file(nested / "deep.py", "NEEDLE\n")
        role = CapRole(cwd=str(sub))
        out = run(bind(Search(), role).call(content="NEEDLE", path="nested")).output
        assert "deep.py" in out


# --- code-map glimpses -------------------------------------------------------


class TestGlimpse:
    def test_files_axis_records_matched_py(self, tree):
        role = CapRole()
        run(bind(Search(), role).call(files="**/*.py"))
        assert any(p.endswith("a.py") for p in role.glimpsed)
        assert any(p.endswith("d.py") for p in role.glimpsed)
        assert all(os.path.isabs(p) for p in role.glimpsed)

    def test_files_axis_non_py_not_recorded(self, tree):
        role = CapRole()
        run(bind(Search(), role).call(files="*.txt"))
        assert role.glimpsed == []

    def test_content_axis_records_matched_py(self, tree):
        role = CapRole()
        run(bind(Search(), role).call(content="ERROR"))  # a.py (py) + c.txt (text)
        assert any(p.endswith("a.py") for p in role.glimpsed)
        assert not any(p.endswith("c.txt") for p in role.glimpsed)

    def test_content_axis_records_across_modes(self, tree):
        role = CapRole()
        run(bind(Search(), role).call(content="foo", output_mode="content"))
        assert any(p.endswith("a.py") for p in role.glimpsed)
        assert any(p.endswith("b.py") for p in role.glimpsed)

    def test_unbound_does_not_raise(self, tree):
        # No Role -> record_file_glimpsed absent -> glimpse pass no-ops.
        assert "a.py" in _search(files="**/*.py")
        assert "a.py:3" in _search(content="ERROR")


# --- Declarations ------------------------------------------------------------


class TestDeclarations:
    def test_read_only_metadata(self):
        # Search is a read-only observation: reconstructable + PURE effect.
        from mote.common.schema import ToolEffect

        assert Search.reconstructable is True
        assert Search.resolve_effect() is ToolEffect.PURE

    def test_summary_is_single_line(self):
        summary = Search.summary()
        assert summary and "\n" not in summary


# --- Shared engine helpers ---------------------------------------------------


class TestHelpers:
    def test_split_glob_brace_preserved(self):
        assert split_glob("*.{ts,tsx}") == ["*.{ts,tsx}"]

    def test_split_glob_comma_and_space(self):
        assert split_glob("*.js,*.ts *.py") == ["*.js", "*.ts", "*.py"]

    def test_apply_head_limit_unlimited(self):
        items = list(range(10))
        sliced, applied = apply_head_limit(items, 0, 0)
        assert sliced == items
        assert applied is None

    def test_apply_head_limit_none_is_unlimited(self):
        items = list(range(500))
        sliced, applied = apply_head_limit(items, None, 0)
        assert sliced == items
        assert applied is None

    def test_apply_head_limit_truncates(self):
        sliced, applied = apply_head_limit(list(range(10)), 3, 0)
        assert sliced == [0, 1, 2]
        assert applied == 3

    def test_apply_head_limit_offset(self):
        sliced, applied = apply_head_limit(list(range(10)), 2, 5)
        assert sliced == [5, 6]
        assert applied == 2

    def test_apply_head_limit_no_truncation_when_within(self):
        sliced, applied = apply_head_limit([1, 2], 5, 0)
        assert sliced == [1, 2]
        assert applied is None

    def test_find_ripgrep_returns_str_or_none(self):
        rg = find_ripgrep()
        assert rg is None or isinstance(rg, str)
