"""Unit tests for the plan gate (code_review/plan.py).

Covers the pure helpers (file-list render, reorder, plan-object extraction) and
``make_plan`` with the child-agent boundary monkeypatched (no real Role / LLM).
"""
from __future__ import annotations

import pytest
from mote.executor.tools.code_review import plan as plan_mod
from mote.executor.tools.code_review._agent import extract_json_object
from mote.executor.tools.code_review.parser import FileDiff, Hunk
from mote.executor.tools.code_review.plan import ReviewPlan, make_plan


def _f(path: str) -> FileDiff:
    return FileDiff(path=path, hunks=[Hunk(new_start=1, lines=[(1, "+x = 1")])])


_FILES = [_f("a.py"), _f("b.py"), _f("c.py")]


class TestExtractPlanObject:
    def test_bare_object(self):
        obj = extract_json_object('{"strategy": "s", "order": ["a.py"]}')
        assert obj == {"strategy": "s", "order": ["a.py"]}

    def test_fenced_json(self):
        text = 'prose\n```json\n{"strategy": "x"}\n```\nmore'
        assert extract_json_object(text) == {"strategy": "x"}

    def test_embedded_span(self):
        text = 'here: {\n  "strategy": "y"\n} done'
        assert extract_json_object(text) == {"strategy": "y"}

    def test_array_not_object_none(self):
        assert extract_json_object("[1, 2]") is None

    def test_unparseable_none(self):
        assert extract_json_object("not json") is None
        assert extract_json_object("") is None


class TestReorder:
    def test_full_permutation(self):
        out = plan_mod._reorder(_FILES, ["c.py", "a.py", "b.py"])
        assert [f.path for f in out] == ["c.py", "a.py", "b.py"]

    def test_partial_order_keeps_tail(self):
        out = plan_mod._reorder(_FILES, ["b.py"])
        assert [f.path for f in out] == ["b.py", "a.py", "c.py"]

    def test_unknown_paths_ignored(self):
        out = plan_mod._reorder(_FILES, ["zzz.py", "c.py"])
        assert [f.path for f in out] == ["c.py", "a.py", "b.py"]

    def test_duplicate_in_order_dropped(self):
        out = plan_mod._reorder(_FILES, ["a.py", "a.py", "b.py"])
        assert [f.path for f in out] == ["a.py", "b.py", "c.py"]


@pytest.mark.asyncio
class TestMakePlan:
    async def test_trivial_changeset_identity(self, monkeypatch):
        called = {"built": False}

        def _no_build(**kwargs):
            called["built"] = True

        monkeypatch.setattr(plan_mod, "build_child_role", _no_build)
        # 0 and 1 files skip the agent entirely.
        p0 = await make_plan([])
        p1 = await make_plan([_f("only.py")])
        assert not called["built"]
        assert p0.ordered == [] and p0.strategy == ""
        assert [f.path for f in p1.ordered] == ["only.py"]

    async def test_happy_path_reorders_and_annotates(self, monkeypatch):
        monkeypatch.setattr(plan_mod, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="plan"):
            return '{"strategy": "watch auth", "order": ["c.py", "a.py", "b.py"]}'

        monkeypatch.setattr(plan_mod, "run_child_for_text", fake_run)
        p = await make_plan(_FILES, repo_dir="/repo")
        assert p.strategy == "watch auth"
        assert [f.path for f in p.ordered] == ["c.py", "a.py", "b.py"]

    async def test_unparseable_degrades_to_identity(self, monkeypatch):
        monkeypatch.setattr(plan_mod, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="plan"):
            return "sorry, no JSON here"

        monkeypatch.setattr(plan_mod, "run_child_for_text", fake_run)
        p = await make_plan(_FILES)
        assert p.strategy == ""
        assert [f.path for f in p.ordered] == ["a.py", "b.py", "c.py"]

    async def test_none_output_degrades(self, monkeypatch):
        monkeypatch.setattr(plan_mod, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="plan"):
            return None

        monkeypatch.setattr(plan_mod, "run_child_for_text", fake_run)
        p = await make_plan(_FILES)
        assert [f.path for f in p.ordered] == ["a.py", "b.py", "c.py"]

    async def test_missing_order_keeps_original(self, monkeypatch):
        monkeypatch.setattr(plan_mod, "build_child_role", lambda **k: object())

        async def fake_run(role, prompt, *, label="plan"):
            return '{"strategy": "just a note"}'

        monkeypatch.setattr(plan_mod, "run_child_for_text", fake_run)
        p = await make_plan(_FILES)
        assert p.strategy == "just a note"
        assert [f.path for f in p.ordered] == ["a.py", "b.py", "c.py"]
