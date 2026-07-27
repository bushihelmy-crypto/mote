#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the pure hunk algebra primitive (:mod:`mote.contracts.text.hunks`).

Ported from the reference ``xai-hunk-tracker`` diff suite, adapted to the pure
value-hunk (geometry + content, no attribution). Covers hunk computation,
the 1-indexed line patch primitive, and apply/revert round-trips with drift
guards.
"""
from __future__ import annotations

import pytest

from mote.contracts.text.hunks import (
    Hunk,
    HunkApplyError,
    apply_hunk,
    apply_hunks,
    patch_lines,
    revert_hunk,
    revert_hunks,
    split_hunks,
)


class TestSplitHunks:
    def test_no_changes_returns_empty(self):
        content = "line 1\nline 2\nline 3\n"
        assert split_hunks(content, content) == []

    def test_single_line_modification(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        hunks = split_hunks(baseline, current)

        assert len(hunks) == 1
        h = hunks[0]
        assert h.old_text == "line 2\n"
        assert h.new_text == "modified\n"
        assert h.old_start == 2
        assert h.new_start == 2
        assert h.old_count == 1
        assert h.new_count == 1

    def test_insertion(self):
        baseline = "line 1\nline 2\n"
        current = "line 1\ninserted\nline 2\n"
        hunks = split_hunks(baseline, current)

        assert len(hunks) == 1
        h = hunks[0]
        assert h.old_text is None
        assert h.new_text == "inserted\n"
        assert h.old_count == 0
        assert h.new_count == 1
        assert h.is_insertion

    def test_deletion(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nline 3\n"
        hunks = split_hunks(baseline, current)

        assert len(hunks) == 1
        h = hunks[0]
        assert h.old_text == "line 2\n"
        assert h.new_text == ""
        assert h.old_count == 1
        assert h.new_count == 0
        assert h.is_deletion

    def test_multiple_hunks(self):
        baseline = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        current = "modified 1\nline 2\nline 3\nline 4\nmodified 5\n"
        hunks = split_hunks(baseline, current)

        assert len(hunks) == 2
        assert hunks[0].old_start == 1
        assert hunks[1].old_start == 5

    def test_after_accept_simulation_positions(self):
        # Diff of a partially-accepted baseline against full current yields the
        # two remaining hunks at their true positions.
        patched_baseline = (
            "line 1\nHUNK_A\nline 3\nline 4\nline 5\nline 6\nline 7\n" "line 8\nline 9\nline 10\nline 11\nline 12\n"
        )
        current = (
            "line 1\nHUNK_A\nline 3\nline 4\nline 5\nline 6\nHUNK_B\n" "line 8\nline 9\nline 10\nHUNK_C\nline 12\n"
        )
        hunks = split_hunks(patched_baseline, current)

        assert len(hunks) == 2
        assert hunks[0].old_start == 7
        assert hunks[0].new_text == "HUNK_B\n"
        assert hunks[1].old_start == 11
        assert hunks[1].new_text == "HUNK_C\n"

    def test_oversize_returns_empty(self):
        from mote.contracts.text.hunks import MAX_DIFF_SIZE_BYTES

        big = "x\n" * (MAX_DIFF_SIZE_BYTES + 10)
        assert split_hunks("small\n", big) == []
        assert split_hunks(big, "small\n") == []

    def test_hunks_compare_by_value(self):
        a = split_hunks("a\nb\n", "a\nc\n")[0]
        b = split_hunks("a\nb\n", "a\nc\n")[0]
        assert a == b  # frozen dataclass equality — no identity


class TestHunkAccessors:
    def test_header(self):
        h = Hunk(2, 1, 2, 1, "old\n", "new\n")
        assert h.header() == "@@ -2,1 +2,1 @@"

    def test_summary(self):
        h = Hunk(2, 1, 2, 2, "old\n", "new a\nnew b\n")
        assert h.summary() == "+2/-1"

    def test_file_created(self):
        h = Hunk.file_created("a\nb\nc\n")
        assert h.old_text is None
        assert h.old_count == 0
        assert h.new_start == 1
        assert h.new_count == 3

    def test_file_deleted(self):
        h = Hunk.file_deleted("a\nb\n")
        assert h.new_text == ""
        assert h.new_count == 0
        assert h.old_count == 2


class TestPatchLines:
    def test_basic_replace(self):
        content = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        assert patch_lines(content, 2, 1, "CHANGED\n") == "line 1\nCHANGED\nline 3\nline 4\nline 5\n"

    def test_no_trailing_newline_in_insert(self):
        content = "line 1\nline 2\nline 3\n"
        assert patch_lines(content, 2, 1, "CHANGED") == "line 1\nCHANGED\nline 3\n"

    def test_pure_insert(self):
        content = "line 1\nline 2\nline 3\n"
        assert patch_lines(content, 2, 0, "INSERTED\n") == "line 1\nINSERTED\nline 2\nline 3\n"

    def test_pure_delete(self):
        content = "line 1\nline 2\nline 3\n"
        assert patch_lines(content, 2, 1, "") == "line 1\nline 3\n"

    def test_multiple_lines(self):
        content = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        assert patch_lines(content, 2, 2, "NEW A\nNEW B\n") == "line 1\nNEW A\nNEW B\nline 4\nline 5\n"

    def test_no_trailing_newline_preserved(self):
        content = "line 1\nline 2"
        assert patch_lines(content, 2, 1, "CHANGED") == "line 1\nCHANGED"


class TestApplyRevert:
    def test_apply_hunk_folds_change_into_baseline(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        assert apply_hunk(baseline, hunk) == current

    def test_revert_hunk_restores_baseline(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        assert revert_hunk(current, hunk) == baseline

    def test_apply_insertion(self):
        baseline = "line 1\nline 2\n"
        current = "line 1\ninserted\nline 2\n"
        (hunk,) = split_hunks(baseline, current)
        assert apply_hunk(baseline, hunk) == current
        assert revert_hunk(current, hunk) == baseline

    def test_apply_deletion(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        assert apply_hunk(baseline, hunk) == current
        assert revert_hunk(current, hunk) == baseline

    def test_apply_verify_raises_on_drift(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        drifted = "wholly\ndifferent\ncontent\n"
        with pytest.raises(HunkApplyError):
            apply_hunk(drifted, hunk)

    def test_revert_verify_raises_on_drift(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        with pytest.raises(HunkApplyError):
            revert_hunk("something\nelse\n", hunk)

    def test_verify_false_skips_drift_guard(self):
        baseline = "line 1\nline 2\nline 3\n"
        current = "line 1\nmodified\nline 3\n"
        (hunk,) = split_hunks(baseline, current)
        # Does not raise even though content differs.
        apply_hunk("a\nb\nc\n", hunk, verify=False)


class TestMultiHunk:
    def test_apply_all_hunks_reproduces_current(self):
        baseline = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        current = "modified 1\nline 2\nline 3\nline 4\nmodified 5\n"
        hunks = split_hunks(baseline, current)
        assert len(hunks) == 2
        assert apply_hunks(baseline, hunks) == current

    def test_revert_all_hunks_reproduces_baseline(self):
        baseline = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        current = "modified 1\nline 2\nline 3\nline 4\nmodified 5\n"
        hunks = split_hunks(baseline, current)
        assert revert_hunks(current, hunks) == baseline

    def test_apply_subset_leaves_other_hunks_valid(self):
        # Accept only the last hunk; re-diffing the patched baseline against
        # current must still surface the first hunk at its original position.
        baseline = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        current = "modified 1\nline 2\nline 3\nline 4\nmodified 5\n"
        hunks = split_hunks(baseline, current)
        last = max(hunks, key=lambda h: h.old_start)
        patched = apply_hunk(baseline, last)
        remaining = split_hunks(patched, current)
        assert len(remaining) == 1
        assert remaining[0].old_start == 1
        assert remaining[0].new_text == "modified 1\n"
