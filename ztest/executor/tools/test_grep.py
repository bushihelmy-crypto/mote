#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Grep tool (``metagpt.executor.tools.grep``).

Exercises the three output modes, glob/type filters, case-insensitivity,
pagination, and the error guards through the public ``call`` (ripgrep is present
in this environment, so this covers the real binary path). ripgrep is a hard
dependency now, so the "rg required" error and the on-demand document-pass
gating are covered too, along with the pure helper functions.
"""
from __future__ import annotations

import os

import pytest

from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools.grep import Grep, _apply_head_limit, _split_glob, _find_ripgrep

from .conftest import run, write_file


def _grep(**kwargs):
    return run(Grep().call(**kwargs))


@pytest.fixture
def tree(workspace):
    """A small file tree to search."""
    write_file(workspace / "a.py", "import os\ndef foo():\n    return ERROR\n")
    write_file(workspace / "b.py", "x = 1\nfoo()\n")
    write_file(workspace / "c.txt", "no match here\nERROR in text\n")
    sub = workspace / "sub"
    sub.mkdir()
    write_file(sub / "d.py", "def bar():\n    pass\n")
    return workspace


class TestFilesWithMatches:
    def test_lists_matching_files_with_lineno(self, tree):
        out = _grep(pattern="ERROR")
        # Default mode emits "path:line" so it can be fed to Read's offset.
        assert "a.py:3" in out
        assert "c.txt:2" in out
        assert "b.py" not in out

    def test_no_matches(self, tree):
        out = _grep(pattern="zzz-not-present-anywhere")
        assert out == "No files found"

    def test_glob_filter(self, tree):
        out = _grep(pattern="ERROR", glob="*.py")
        assert "a.py:3" in out
        assert "c.txt" not in out

    def test_type_filter(self, tree):
        out = _grep(pattern="ERROR", type="py")
        assert "a.py" in out
        assert "c.txt" not in out


class TestContentMode:
    def test_content_with_line_numbers(self, tree):
        out = _grep(pattern="ERROR", output_mode="content")
        assert "a.py:3:" in out
        assert "return ERROR" in out

    def test_case_insensitive(self, tree):
        write_file(tree / "e.py", "lowercase error\n")
        out = _grep(pattern="error", output_mode="content", case_insensitive=True)
        assert "e.py" in out
        # uppercase ERROR lines also match case-insensitively
        assert "a.py" in out

    def test_context_lines(self, tree):
        out = _grep(pattern="return ERROR", output_mode="content", context=1)
        # Context pulls in the surrounding def line.
        assert "def foo" in out


class TestCountMode:
    def test_per_file_counts_and_summary(self, tree):
        write_file(tree / "many.txt", "ERROR\nERROR\nERROR\n")
        out = _grep(pattern="ERROR", output_mode="count")
        assert "many.txt:3" in out
        assert "Found" in out and "occurrences" in out


class TestPagination:
    def test_head_limit_truncates(self, workspace):
        for i in range(10):
            write_file(workspace / f"f{i}.txt", "ERROR\n")
        out = _grep(pattern="ERROR", head_limit=3)
        # 3 result lines + the "Found ..." header.
        body = [ln for ln in out.splitlines() if ln and not ln.startswith("Found")]
        assert len(body) == 3
        assert "limit: 3" in out

    def test_offset_paginates(self, workspace):
        for i in range(5):
            write_file(workspace / f"f{i}.txt", "ERROR\n")
        out = _grep(pattern="ERROR", head_limit=2, offset=2)
        assert "offset: 2" in out


class TestGuards:
    def test_empty_pattern_raises(self, workspace):
        with pytest.raises(ToolError, match="'pattern' argument is required"):
            _grep(pattern="  ")

    def test_invalid_output_mode_raises(self, workspace):
        with pytest.raises(ToolError, match="invalid output_mode"):
            _grep(pattern="x", output_mode="bogus")

    def test_missing_path_raises(self, workspace):
        with pytest.raises(ToolError, match="path does not exist"):
            _grep(pattern="x", path=str(workspace / "nope"))


# --- ripgrep hard dependency (no in-process text fallback) -------------------


class TestRipgrepRequired:
    def test_missing_rg_raises_for_text_search(self, tree, monkeypatch):
        # With no ripgrep available, a text search must fail loudly rather than
        # silently walking the tree in-process (the old fallback froze the loop).
        import metagpt.executor.tools.grep as grep_mod

        monkeypatch.setattr(grep_mod, "_find_ripgrep", lambda: None)
        with pytest.raises(ToolError, match="is required for text search"):
            _grep(pattern="ERROR")

    def test_doc_only_type_works_without_rg(self, workspace, monkeypatch):
        # A doc-only type (pdf) never needs rg, so it must not raise even when
        # ripgrep is absent — it goes straight to the document-extraction pass.
        import metagpt.executor.tools.grep as grep_mod

        monkeypatch.setattr(grep_mod, "_find_ripgrep", lambda: None)
        out = _grep(pattern="anything", type="pdf")
        assert out == "No files found"  # no PDFs present, but no "rg required" error


# --- On-demand document pass gating ------------------------------------------


class TestDocumentGating:
    def test_plain_code_search_skips_documents(self, tree):
        # The common case: no doc type, no doc glob -> document walk not triggered.
        assert Grep._query_targets_documents(str(tree), "", "") is False

    def test_doc_type_triggers(self, tree):
        assert Grep._query_targets_documents(str(tree), "", "pdf") is True

    def test_doc_glob_triggers(self, tree):
        assert Grep._query_targets_documents(str(tree), "*.pdf", "") is True
        # Brace groups are recognized via bare-extension substring matching.
        assert Grep._query_targets_documents(str(tree), "*.{docx,xlsx}", "") is True

    def test_single_document_file_triggers(self, workspace):
        write_file(workspace / "report.pdf", "unused")
        assert Grep._query_targets_documents(str(workspace / "report.pdf"), "", "") is True

    def test_single_code_file_does_not_trigger(self, tree):
        assert Grep._query_targets_documents(str(tree / "a.py"), "", "") is False


# --- Vendored ripgrep binary -------------------------------------------------


class TestVendoredRipgrep:
    def test_vendored_binary_is_present_and_executable(self):
        # x86_64-linux is checked in; assert it exists there so a regression in
        # packaging is caught. On other platforms this path simply won't exist.
        import metagpt.executor.tools.grep as grep_mod

        if os.name == "posix" and "x86_64-linux" in grep_mod._VENDORED_RIPGREP:
            assert os.path.isfile(grep_mod._VENDORED_RIPGREP)
            assert os.access(grep_mod._VENDORED_RIPGREP, os.X_OK)


# --- Pure helpers ------------------------------------------------------------


class TestHelpers:
    def test_split_glob_brace_preserved(self):
        assert _split_glob("*.{ts,tsx}") == ["*.{ts,tsx}"]

    def test_split_glob_comma_and_space(self):
        assert _split_glob("*.js,*.ts *.py") == ["*.js", "*.ts", "*.py"]

    def test_apply_head_limit_unlimited(self):
        items = list(range(10))
        sliced, applied = _apply_head_limit(items, 0, 0)
        assert sliced == items
        assert applied is None

    def test_apply_head_limit_truncates(self):
        items = list(range(10))
        sliced, applied = _apply_head_limit(items, 3, 0)
        assert sliced == [0, 1, 2]
        assert applied == 3

    def test_apply_head_limit_offset(self):
        items = list(range(10))
        sliced, applied = _apply_head_limit(items, 2, 5)
        assert sliced == [5, 6]
        assert applied == 2

    def test_apply_head_limit_no_truncation_when_within(self):
        sliced, applied = _apply_head_limit([1, 2], 5, 0)
        assert sliced == [1, 2]
        assert applied is None

    def test_find_ripgrep_returns_str_or_none(self):
        # Just exercise the probe; either a path or None is acceptable.
        rg = _find_ripgrep()
        assert rg is None or isinstance(rg, str)
