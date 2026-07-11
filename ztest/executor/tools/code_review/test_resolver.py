"""Unit tests for the comment resolver (code_review/resolver.py)."""
from __future__ import annotations

from mote.executor.tools.code_review.parser import FileDiff, Hunk
from mote.executor.tools.code_review.resolver import resolve_comment


def _file_with_lines(lines):
    """Build a FileDiff with one hunk from (lineno, text) tuples."""
    return FileDiff(path="x.py", hunks=[Hunk(new_start=lines[0][0], lines=lines)])


# new-side lines: context lines prefixed " ", added lines "+".
_FILE = _file_with_lines(
    [
        (10, " def handler(req):"),
        (11, "+    token = req.get('token')"),
        (12, "+    if token == 'admin':"),
        (13, "+        grant_access()"),
        (14, "     return None"),
    ]
)


class TestSingleLine:
    def test_hit(self):
        span = resolve_comment("        grant_access()", _FILE)
        assert span == (13, 13)

    def test_hit_added_line(self):
        span = resolve_comment("    token = req.get('token')", _FILE)
        assert span == (11, 11)

    def test_miss(self):
        assert resolve_comment("nonexistent_call()", _FILE) is None


class TestMultiLine:
    def test_contiguous_run(self):
        snippet = "    if token == 'admin':\n        grant_access()"
        span = resolve_comment(snippet, _FILE)
        assert span == (12, 13)

    def test_full_block(self):
        snippet = "    token = req.get('token')\n" "    if token == 'admin':\n" "        grant_access()"
        span = resolve_comment(snippet, _FILE)
        assert span == (11, 13)

    def test_non_contiguous_miss(self):
        # These two lines are not adjacent in the source.
        snippet = "def handler(req):\n        grant_access()"
        assert resolve_comment(snippet, _FILE) is None


class TestNormalization:
    def test_trailing_whitespace_ignored(self):
        span = resolve_comment("        grant_access()    \n", _FILE)
        assert span == (13, 13)

    def test_leading_trailing_blank_lines_trimmed(self):
        snippet = "\n\n        grant_access()\n\n"
        span = resolve_comment(snippet, _FILE)
        assert span == (13, 13)

    def test_empty_snippet_none(self):
        assert resolve_comment("", _FILE) is None
        assert resolve_comment("   \n  ", _FILE) is None

    def test_no_hunks_none(self):
        empty = FileDiff(path="x.py", hunks=[])
        assert resolve_comment("anything", empty) is None


class TestFuzzyFallback:
    """RE_LOCATION: near-miss snippets resolve via difflib similarity."""

    def test_single_line_reworded_hit(self):
        # Agent lightly reworded a single line (kept most tokens).
        span = resolve_comment("token = req.get('token')  # auth", _FILE)
        assert span == (11, 11)

    def test_single_line_unrelated_miss(self):
        # A genuinely different line stays below threshold → None.
        assert resolve_comment("completely_other_function(x, y, z)", _FILE) is None

    def test_multi_line_minor_drift_hit(self):
        # Two adjacent lines with slight whitespace/token drift.
        snippet = "if token == 'admin' :\n        grant_access ()"
        span = resolve_comment(snippet, _FILE)
        assert span == (12, 13)

    def test_multi_line_non_contiguous_still_miss(self):
        # Scattered lines must not be relocated onto one of them.
        snippet = "def handler(req):\n    grant_access()  # reworded"
        assert resolve_comment(snippet, _FILE) is None
