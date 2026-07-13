"""Integration tests for the code-review graph (code_review/graph.py).

Drives the compiled graph end-to-end with the I/O boundaries monkeypatched:
``get_diff`` returns a fixed diff and ``review_one_file`` returns canned
findings — no real Role / LLM / git. Exercises the ring+batch topology
(multiple waves), the findings reducer merge, and report formatting.
"""
from __future__ import annotations

import pytest

from mote.executor.tasks.bggraph import END, START, BgGraph
from mote.executor.tools.code_review import nodes as nodes_mod
from mote.executor.tools.code_review.format import Finding
from mote.executor.tools.code_review.graph import build_code_review_graph
from mote.executor.tools.code_review.parser import FileDiff, Hunk
from mote.executor.tools.code_review.plan import ReviewPlan


def _is_bg_task_result(obj) -> bool:
    return type(obj).__name__ == "BgTaskResult" and hasattr(obj, "poll_factory")


# A diff with 5 reviewable .py files (forces multiple waves at batch_size=2).
_DIFF = "".join(
    f"""\
diff --git a/f{i}.py b/f{i}.py
index 1111111..2222222 100644
--- a/f{i}.py
+++ b/f{i}.py
@@ -1,1 +1,2 @@
 x = {i}
+y = {i}
"""
    for i in range(5)
)


async def _run(graph: BgGraph, **inputs):
    # White-box: run inline via ``arun`` and dump the *full* final state so these
    # topology/reducer tests can inspect intermediate fields (findings/remaining/
    # kept_findings). The background poll path narrows the success result to the
    # declared ``Output`` (report only) — that narrowing is covered by
    # test_output_fields.py and ``test_compile_narrows_to_report`` below.
    state = await graph.arun(**inputs)
    return state.model_dump()


@pytest.fixture
def patched(monkeypatch):
    """Patch the I/O + agent boundaries: fixed diff + one finding per file.

    The plan and review_filter nodes call child Roles too; patch them to
    deterministic identity behavior so the graph test stays hermetic (no real
    Role / LLM anywhere).
    """
    calls = {"reviewed": [], "planned": 0, "filtered": 0}

    async def fake_get_diff(repo_dir, **kwargs):
        return _DIFF

    async def fake_review_one_file(file_diff, repo_dir, parent_session_id=""):
        calls["reviewed"].append(file_diff.path)
        return [
            Finding(
                file=file_diff.path,
                severity="warning",
                message=f"issue in {file_diff.path}",
                existing_code="y = 0",
                start_line=2,
                end_line=2,
            )
        ]

    async def fake_make_plan(files, repo_dir="", parent_session_id=""):
        calls["planned"] += 1
        return ReviewPlan(strategy="be careful", ordered=list(files))

    async def fake_filter_findings(findings, repo_dir="", parent_session_id=""):
        calls["filtered"] += 1
        return list(findings)

    monkeypatch.setattr(nodes_mod, "get_diff", fake_get_diff)
    monkeypatch.setattr(nodes_mod, "review_one_file", fake_review_one_file)
    monkeypatch.setattr(nodes_mod, "make_plan", fake_make_plan)
    monkeypatch.setattr(nodes_mod, "filter_findings", fake_filter_findings)
    return calls


class TestTopology:
    def test_build_nodes(self):
        g = build_code_review_graph()
        assert set(g._nodes.keys()) == {
            "load_diff",
            "parse_filter",
            "plan",
            "review_batch",
            "review_filter",
            "aggregate",
        }

    def test_compiles(self):
        assert callable(build_code_review_graph().compile())


@pytest.mark.asyncio
class TestRingBatch:
    async def test_all_files_reviewed_across_waves(self, patched):
        g = build_code_review_graph()
        out = await _run(
            g,
            repo_dir="/repo",
            from_ref=None,
            to_ref=None,
            commit=None,
            batch_size=2,
            fmt="text",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            findings=[],
            report="",
        )
        # All 5 files reviewed (3 waves: 2 + 2 + 1).
        assert sorted(patched["reviewed"]) == [f"f{i}.py" for i in range(5)]

    async def test_findings_reducer_merges(self, patched):
        g = build_code_review_graph()
        out = await _run(
            g,
            repo_dir="/repo",
            batch_size=2,
            fmt="json",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            findings=[],
            report="",
        )
        # 5 findings accumulated across 3 batches via operator.add.
        assert len(out["findings"]) == 5

    async def test_report_non_empty(self, patched):
        g = build_code_review_graph()
        out = await _run(
            g,
            repo_dir="/repo",
            batch_size=2,
            fmt="text",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            findings=[],
            report="",
        )
        assert out["report"]
        assert "found 5 issues" in out["report"]
        assert "remaining" in out and out["remaining"] == []

    async def test_single_wave_when_batch_covers_all(self, patched):
        g = build_code_review_graph()
        out = await _run(
            g,
            repo_dir="/repo",
            batch_size=10,
            fmt="text",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            findings=[],
            report="",
        )
        assert len(patched["reviewed"]) == 5
        assert "found 5 issues" in out["report"]

    async def test_compile_narrows_to_report(self, patched):
        # The background poll path returns only the declared ``Output`` field
        # (``report``) — intermediate scratch (findings/remaining/kept_findings)
        # never leaks into what is pushed to the model on success.
        g = build_code_review_graph()
        res = await g.compile()(repo_dir="/repo", batch_size=2, fmt="text")
        assert _is_bg_task_result(res)
        out = await res.poll_factory()
        assert set(out) == {"report"}
        assert "found 5 issues" in out["report"]


@pytest.mark.asyncio
class TestEmptyDiff:
    async def test_no_files(self, monkeypatch):
        async def empty_diff(repo_dir, **kwargs):
            return ""

        reviewed = []

        async def should_not_run(*a, **k):
            reviewed.append(1)
            return []

        monkeypatch.setattr(nodes_mod, "get_diff", empty_diff)
        monkeypatch.setattr(nodes_mod, "review_one_file", should_not_run)

        g = build_code_review_graph()
        out = await _run(
            g,
            repo_dir="/repo",
            batch_size=2,
            fmt="text",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            findings=[],
            report="",
        )
        assert reviewed == []
        assert out["findings"] == []
        assert "no issues found" in out["report"].lower()


@pytest.mark.asyncio
class TestPlanAndFilter:
    async def test_plan_and_filter_run_once(self, patched):
        g = build_code_review_graph()
        await _run(g, repo_dir="/repo", batch_size=2, fmt="text")
        # Plan runs once before the ring; review_filter runs once after it.
        assert patched["planned"] == 1
        assert patched["filtered"] == 1

    async def test_strategy_in_text_report(self, patched):
        g = build_code_review_graph()
        out = await _run(g, repo_dir="/repo", batch_size=2, fmt="text")
        # The plan's strategy note is prepended to the text report.
        assert "Review strategy: be careful" in out["report"]

    async def test_strategy_absent_from_json(self, patched):
        g = build_code_review_graph()
        out = await _run(g, repo_dir="/repo", batch_size=10, fmt="json")
        # JSON output is a pure findings array — no strategy preamble.
        assert out["report"].lstrip().startswith("[")

    async def test_filter_prunes_findings(self, monkeypatch):
        async def fake_get_diff(repo_dir, **kwargs):
            return _DIFF

        async def fake_review_one_file(file_diff, repo_dir, parent_session_id=""):
            return [
                Finding(
                    file=file_diff.path,
                    severity="info",
                    message=f"nit in {file_diff.path}",
                    existing_code="y = 0",
                    start_line=2,
                    end_line=2,
                )
            ]

        async def fake_make_plan(files, repo_dir="", parent_session_id=""):
            return ReviewPlan(strategy="", ordered=list(files))

        async def drop_all(findings, repo_dir="", parent_session_id=""):
            return []  # critique drops every finding

        monkeypatch.setattr(nodes_mod, "get_diff", fake_get_diff)
        monkeypatch.setattr(nodes_mod, "review_one_file", fake_review_one_file)
        monkeypatch.setattr(nodes_mod, "make_plan", fake_make_plan)
        monkeypatch.setattr(nodes_mod, "filter_findings", drop_all)

        g = build_code_review_graph()
        out = await _run(g, repo_dir="/repo", batch_size=2, fmt="text")
        # 5 raw findings accumulated, but the filter kept none → empty report.
        assert len(out["findings"]) == 5
        assert out["kept_findings"] == []
        assert "no issues found" in out["report"].lower()
