"""Unit tests for findings formatting (code_review/format.py)."""
from __future__ import annotations

import json

from mote.executor.tools.code_review.format import Finding, format_findings

_FINDINGS = [
    Finding(
        file="a.py",
        severity="critical",
        message="SQL injection risk",
        existing_code="q = 'SELECT '+x",
        start_line=12,
        end_line=12,
    ),
    Finding(
        file="a.py",
        severity="warning",
        message="unused variable",
        existing_code="y = 1",
        start_line=20,
        end_line=22,
    ),
    Finding(
        file="b.go",
        severity="info",
        message="consider early return",
        existing_code="if x {}",
        start_line=None,
        end_line=None,
    ),
]


class TestText:
    def test_groups_by_file(self):
        out = format_findings(_FINDINGS, fmt="text")
        assert "## a.py" in out
        assert "## b.go" in out
        # a.py group appears before b.go (first-seen order).
        assert out.index("## a.py") < out.index("## b.go")

    def test_line_labels(self):
        out = format_findings(_FINDINGS, fmt="text")
        assert "a.py:L12  [critical] SQL injection risk" in out
        assert "a.py:L20-22  [warning] unused variable" in out
        assert "b.go:L?  [info] consider early return" in out

    def test_summary_header(self):
        out = format_findings(_FINDINGS, fmt="text")
        assert "found 3 issues across 2 files" in out

    def test_empty(self):
        out = format_findings([], fmt="text")
        assert "no issues found" in out.lower()


class TestJson:
    def test_shape(self):
        out = format_findings(_FINDINGS, fmt="json")
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 3
        assert data[0]["file"] == "a.py"
        assert data[0]["severity"] == "critical"
        assert data[0]["start_line"] == 12
        assert data[2]["start_line"] is None

    def test_empty_json(self):
        out = format_findings([], fmt="json")
        assert json.loads(out) == []
