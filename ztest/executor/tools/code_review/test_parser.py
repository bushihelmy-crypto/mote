"""Unit tests for the unified-diff parser (code_review/parser.py)."""
from __future__ import annotations

from metagpt.executor.tools.code_review.parser import parse_unified_diff


_MODIFIED_DIFF = """\
diff --git a/foo.py b/foo.py
index 1234567..89abcde 100644
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,5 @@
 import os
-import sys
+import sys  # changed
+import json

 def main():
@@ -10,2 +11,3 @@ def main():
     x = 1
+    y = 2
     return x
"""

_NEW_FILE_DIFF = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+def hello():
+    return "hi"
+
"""

_DELETED_FILE_DIFF = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index e69de29..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def old():
-    pass
"""

_BINARY_DIFF = """\
diff --git a/img.png b/img.png
index 1111111..2222222 100644
Binary files a/img.png and b/img.png differ
"""


class TestParseModified:
    def test_single_file(self):
        files = parse_unified_diff(_MODIFIED_DIFF)
        assert len(files) == 1
        f = files[0]
        assert f.path == "foo.py"
        assert not f.is_binary
        assert not f.is_new
        assert not f.is_deleted
        assert len(f.hunks) == 2

    def test_new_side_line_numbers(self):
        f = parse_unified_diff(_MODIFIED_DIFF)[0]
        # First hunk starts at new line 1: context "import os" (1), added
        # "import sys  # changed" (2), added "import json" (3), blank (4),
        # context "def main():" (5). The removed "import sys" has no new line.
        first = f.hunks[0]
        assert first.new_start == 1
        nums = [ln for ln, _ in first.lines]
        assert nums == [1, 2, 3, 4, 5]
        texts = [t for _, t in first.lines]
        assert texts[0] == " import os"
        assert texts[1] == "+import sys  # changed"
        assert texts[2] == "+import json"

    def test_second_hunk_offset(self):
        f = parse_unified_diff(_MODIFIED_DIFF)[0]
        second = f.hunks[1]
        assert second.new_start == 11
        nums = [ln for ln, _ in second.lines]
        # context x=1 (11), added y=2 (12), context return x (13)
        assert nums == [11, 12, 13]

    def test_added_count(self):
        f = parse_unified_diff(_MODIFIED_DIFF)[0]
        assert f.added_count() == 3  # two in hunk1, one in hunk2


class TestNewAndDeleted:
    def test_new_file_flag(self):
        f = parse_unified_diff(_NEW_FILE_DIFF)[0]
        assert f.is_new
        assert f.path == "new.py"
        nums = [ln for ln, _ in f.hunks[0].lines]
        assert nums == [1, 2, 3]

    def test_deleted_file_flag(self):
        f = parse_unified_diff(_DELETED_FILE_DIFF)[0]
        assert f.is_deleted
        assert f.path == "gone.py"

    def test_binary_flag(self):
        f = parse_unified_diff(_BINARY_DIFF)[0]
        assert f.is_binary
        assert not f.hunks


class TestMultiFile:
    def test_multiple_files(self):
        text = _MODIFIED_DIFF + _NEW_FILE_DIFF + _BINARY_DIFF
        files = parse_unified_diff(text)
        assert [f.path for f in files] == ["foo.py", "new.py", "img.png"]
        assert files[1].is_new
        assert files[2].is_binary

    def test_empty_input(self):
        assert parse_unified_diff("") == []
