#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.compress.git.GitCompressor``."""
from __future__ import annotations

from metagpt.executor.compress.git import GitCompressor

STATUS = """On branch master
Your branch is up to date with 'origin/master'.

Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
\tmodified:   a.py
\tmodified:   b.py

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
\tmodified:   c.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
""" + "\n".join(f"\tnew_{i}.py" for i in range(30))

LOG = "\n".join(
    f"""commit {'a' * 40}{i}
Author: Someone <s@example.com>
Date:   Mon Jan 1 00:00:00 2026 +0000

    Subject line {i}

    Body paragraph that is quite long and should be dropped entirely
    across multiple lines of prose that add no signal for the model.
"""
    for i in range(10)
)

DIFF = """diff --git a/foo.py b/foo.py
index 1234567..89abcde 100644
--- a/foo.py
+++ b/foo.py
@@ -1,5 +1,7 @@
 context line
""" + "\n".join(f"+added line {i}" for i in range(40)) + "\n" + "\n".join(
    f"-removed line {i}" for i in range(40)
) + """
diff --git a/old.py b/new.py
similarity index 95%
rename from old.py
rename to new.py
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


class TestStatus:
    def test_shrinks_and_caps_entries(self):
        r = GitCompressor().compress(STATUS, argv=["git", "status"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert r.label == "git status"
        # Section headers survive.
        assert "Changes to be committed:" in r.text
        assert "Untracked files:" in r.text
        # Hint lines are dropped.
        assert "(use " not in r.text
        # The 30 untracked entries are capped with a summary.
        assert "... and 20 more" in r.text

    def test_first_paths_visible(self):
        r = GitCompressor().compress(STATUS, argv=["git", "status"])
        assert "a.py" in r.text
        assert "c.py" in r.text


class TestLog:
    def test_keeps_headers_drops_body(self):
        r = GitCompressor().compress(LOG, argv=["git", "log"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert r.text.count("commit ") == 10  # every commit header kept
        assert "Author: Someone" in r.text
        assert "Subject line 0" in r.text
        # Body prose is dropped.
        assert "Body paragraph" not in r.text

    def test_oneline_kept_verbatim(self):
        oneline = "\n".join(f"{'a' * 7}{i} subject {i}" for i in range(20)) + "\n"
        r = GitCompressor().compress(oneline, argv=["git", "log"])
        # Every oneline row survives (grow-guard may mark it unchanged, but the
        # rows must not be dropped either way).
        for i in range(20):
            assert f"subject {i}" in r.text


class TestDiff:
    def test_budgets_body_keeps_structure(self):
        r = GitCompressor().compress(DIFF, argv=["git", "diff"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert r.label == "git diff"
        # Structural lines survive.
        assert "diff --git a/foo.py b/foo.py" in r.text
        assert "@@ -1,5 +1,7 @@" in r.text
        assert "rename from old.py" in r.text
        assert "rename to new.py" in r.text
        assert "Binary files a/img.png and b/img.png differ" in r.text
        # Body is budgeted with a per-file count line.
        assert "changed lines omitted" in r.text

    def test_counts_added_removed(self):
        r = GitCompressor().compress(DIFF, argv=["git", "diff"])
        # 40 added + 40 removed + 1 context = beyond budget; total reported.
        assert "+41 -40" in r.text or "+40 -40" in r.text
