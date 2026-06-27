"""Unit tests for the self-critique filter (code_review/review_filter.py).

Covers the pure helpers (comment render, keep-index parse) and ``filter_findings``
with the child-agent boundary monkeypatched (no real Role / LLM).
"""
from __future__ import annotations

import pytest

from metagpt.executor.tools.code_review import review_filter as rf
from metagpt.executor.tools.code_review.format import Finding


def _find(msg: str, sev: str = "warning") -> Finding:
    return Finding(
        file="x.py",
        severity=sev,
        message=msg,
        existing_code="y = 0",
        start_line=1,
        end_line=1,
    )


_FINDINGS = [_find("a"), _find("b"), _find("c")]


class TestParseKeepIndices:
    def test_valid_subset(self):
        assert rf._parse_keep_indices("[0, 2]", 3) == [0, 2]

    def test_out_of_range_dropped(self):
        assert rf._parse_keep_indices("[0, 5, 2]", 3) == [0, 2]

    def test_duplicates_deduped(self):
        assert rf._parse_keep_indices("[1, 1, 2]", 3) == [1, 2]

    def test_empty_keep_none(self):
        assert rf._parse_keep_indices("[]", 3) == []

    def test_non_int_skipped(self):
        assert rf._parse_keep_indices('[0, "x", 2]', 3) == [0, 2]

    def test_unparseable_returns_none(self):
        assert rf._parse_keep_indices("not json", 3) is None


class TestRenderComments:
    def test_numbered_lines(self):
        rendered = rf._render_comments(_FINDINGS)
        lines = rendered.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("0: ")
        assert lines[2].startswith("2: ")


@pytest.mark.asyncio
class TestFilterFindings:
    async def test_trivial_passthrough(self, monkeypatch):
        called = {"built": False}
        monkeypatch.setattr(
            rf, "build_child_role", lambda **k: called.__setitem__("built", True)
        )
        # 0 or 1 findings skip the agent.
        assert await rf.filter_findings([]) == []
        one = [_find("solo")]
        out = await rf.filter_findings(one)
        assert [f.message for f in out] == ["solo"]
        assert not called["built"]

    async def test_keeps_subset(self, monkeypatch):
        monkeypatch.setattr(rf, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="review_filter"):
            return "[0, 2]"

        monkeypatch.setattr(rf, "run_child_for_text", fake_run)
        out = await rf.filter_findings(_FINDINGS, repo_dir="/repo")
        assert [f.message for f in out] == ["a", "c"]

    async def test_drop_all(self, monkeypatch):
        monkeypatch.setattr(rf, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="review_filter"):
            return "[]"

        monkeypatch.setattr(rf, "run_child_for_text", fake_run)
        out = await rf.filter_findings(_FINDINGS)
        assert out == []

    async def test_unparseable_keeps_all(self, monkeypatch):
        monkeypatch.setattr(rf, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="review_filter"):
            return "no json"

        monkeypatch.setattr(rf, "run_child_for_text", fake_run)
        out = await rf.filter_findings(_FINDINGS)
        assert [f.message for f in out] == ["a", "b", "c"]

    async def test_none_output_keeps_all(self, monkeypatch):
        monkeypatch.setattr(rf, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="review_filter"):
            return None

        monkeypatch.setattr(rf, "run_child_for_text", fake_run)
        out = await rf.filter_findings(_FINDINGS)
        assert [f.message for f in out] == ["a", "b", "c"]
