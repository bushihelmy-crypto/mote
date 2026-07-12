"""Unit tests for the review unit (code_review/review_unit.py).

Covers the pure helpers (JSON extraction, comment→finding conversion with line
resolution) and the ``review_one_file`` flow with a fake Role (no real LLM).
"""
from __future__ import annotations

import types

import pytest

from mote.common.agent_control import SpawnContext, set_control
from mote.executor.tools.code_review import review_unit as ru
from mote.executor.tools.code_review.parser import FileDiff, Hunk

_FILE = FileDiff(
    path="x.py",
    hunks=[
        Hunk(
            new_start=1,
            lines=[
                (1, " def f():"),
                (2, "+    danger()"),
                (3, "     return 1"),
            ],
        )
    ],
)


class TestExtractJson:
    def test_bare_array(self):
        assert ru._extract_json_array('[{"a": 1}]') == [{"a": 1}]

    def test_fenced_json(self):
        text = 'Here you go:\n```json\n[{"a": 1}]\n```\nthanks'
        assert ru._extract_json_array(text) == [{"a": 1}]

    def test_fenced_plain(self):
        text = "```\n[1, 2, 3]\n```"
        assert ru._extract_json_array(text) == [1, 2, 3]

    def test_embedded_bracket_span(self):
        text = 'prose before [\n  {"a": 1}\n] prose after'
        assert ru._extract_json_array(text) == [{"a": 1}]

    def test_empty_array(self):
        assert ru._extract_json_array("[]") == []

    def test_unparseable_none(self):
        assert ru._extract_json_array("no json here") is None
        assert ru._extract_json_array("") is None

    def test_object_not_array_none(self):
        assert ru._extract_json_array('{"a": 1}') is None


class TestCommentsToFindings:
    def test_resolves_line(self):
        comments = [{"existing_code": "    danger()", "severity": "critical", "message": "bad"}]
        findings = ru._comments_to_findings(comments, _FILE)
        assert len(findings) == 1
        f = findings[0]
        assert f.file == "x.py"
        assert f.severity == "critical"
        assert f.message == "bad"
        assert (f.start_line, f.end_line) == (2, 2)

    def test_unresolved_keeps_none(self):
        comments = [{"existing_code": "nonexistent", "severity": "info", "message": "hmm"}]
        findings = ru._comments_to_findings(comments, _FILE)
        assert findings[0].start_line is None

    def test_skips_empty_message(self):
        comments = [{"existing_code": "    danger()", "message": ""}]
        assert ru._comments_to_findings(comments, _FILE) == []

    def test_skips_non_dict(self):
        comments = ["not a dict", {"message": "ok", "existing_code": "    danger()"}]
        findings = ru._comments_to_findings(comments, _FILE)
        assert len(findings) == 1

    def test_default_severity(self):
        comments = [{"existing_code": "    danger()", "message": "x"}]
        assert ru._comments_to_findings(comments, _FILE)[0].severity == "info"


class TestRenderRelated:
    def test_no_related_blank(self):
        assert ru._render_related(_FILE) == ""

    def test_related_listed(self):
        f = FileDiff(path="x.py", related=["x_test.py", "helper.py"])
        block = ru._render_related(f)
        assert "x_test.py" in block
        assert "helper.py" in block

    def test_related_in_prompt(self):
        f = FileDiff(
            path="x.py",
            hunks=[Hunk(new_start=1, lines=[(1, "+danger()")])],
            related=["x_test.py"],
        )
        prompt = ru._USER_PROMPT_TEMPLATE.format(path=f.path, diff="...", related=ru._render_related(f))
        assert "x_test.py" in prompt


class _FakeRole:
    """Minimal Role stand-in: returns canned final output, tracks cleanup."""

    def __init__(self, output):
        self._output = output
        self.ran = False
        self.cleaned = False

        class _State:
            last_end_output = output

        self.state = _State()

    async def run(self, with_message=None):
        self.ran = True
        return None

    async def cleanup(self):
        self.cleaned = True


class _InlineHandle:
    """Runs the spawned role inline and tears it down — like the real handle."""

    def __init__(self, role):
        self.runtime = types.SimpleNamespace(role=role)
        self._role = role

    async def run_to_completion(self, message):
        try:
            await self._role.run(with_message=message)
            return (getattr(self._role.state, "last_end_output", "") or "").strip()
        finally:
            await self._role.cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _InlineControl:
    """Minimal control plane: builds the child via the spec factory and runs it."""

    async def spawn_agent(self, spec):
        return _InlineHandle(spec.role_factory(SpawnContext()))


@pytest.mark.asyncio
class TestReviewOneFile:
    async def test_happy_path(self, monkeypatch):
        output = '[{"existing_code": "    danger()", "severity": "warning", "message": "risky call"}]'
        fake = _FakeRole(output)
        monkeypatch.setattr(ru, "_build_review_role", lambda repo_dir, psid="": fake)

        with set_control(_InlineControl()):
            findings = await ru.review_one_file(_FILE, "/repo", parent_session_id="p")
        assert fake.ran and fake.cleaned
        assert len(findings) == 1
        assert findings[0].message == "risky call"
        assert findings[0].start_line == 2

    async def test_unparseable_output_empty(self, monkeypatch):
        fake = _FakeRole("I could not find any issues, sorry.")
        monkeypatch.setattr(ru, "_build_review_role", lambda repo_dir, psid="": fake)
        with set_control(_InlineControl()):
            findings = await ru.review_one_file(_FILE, "/repo")
        assert findings == []
        assert fake.cleaned

    async def test_run_failure_propagates_but_still_cleans_up(self, monkeypatch):
        # review_one_file no longer swallows run failures: they propagate so a
        # structural bug surfaces. Per-file isolation lives in the batch node's
        # _safe_review, not here. The child is still torn down (handle finally).
        class _BoomRole(_FakeRole):
            async def run(self, with_message=None):
                raise RuntimeError("llm down")

        fake = _BoomRole("[]")
        monkeypatch.setattr(ru, "_build_review_role", lambda repo_dir, psid="": fake)
        with set_control(_InlineControl()):
            with pytest.raises(RuntimeError):
                await ru.review_one_file(_FILE, "/repo")
        assert fake.cleaned
