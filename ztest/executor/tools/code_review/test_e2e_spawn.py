"""Full-graph end-to-end test for the code-review pipeline.

Unlike ``test_graph.py`` (which fakes away ``review_one_file``), this drives the
REAL spawn path that the original bug lived on:

    graph.poll -> review_batch -> _safe_review -> review_one_file
        -> run_child -> spawn_and_run(ambient plane)
        -> AgentControl.spawn_agent -> _provision_context (FRESH)
        -> build_child_role (context-less Role) -> Role.run() -> router -> LLM

Only two boundaries are faked:
  * ``get_diff`` returns a canned 2-file diff (no real git).
  * ``create_llm_instance`` returns a :class:`ScriptedLLM` so every spawned
    child's router resolves to a deterministic, offline LLM that ends its turn
    with a JSON array of findings.

A live :class:`AgentControl` is bound as the ambient plane (mirroring what the
scheduler does around every turn), so the deep ``run_child`` spawn site resolves
it via ``current_control()``.
"""
from __future__ import annotations

import json

import pytest

# Import the roles package so its ChildRoleBuilder registers into the
# common-layer holder; the deep spawn path (build_child_role) requires it.
import metagpt.roles  # noqa: F401
from metagpt.common.agent_control import set_control
from metagpt.environment.control import AgentControl
from metagpt.environment.store import ResidencyStore
from metagpt.executor.tools.code_review import nodes as nodes_mod
from metagpt.executor.tools.code_review.graph import build_code_review_graph
from metagpt.router.llm.llm_response import LLMResponse


# A diff with 2 reviewable .py files.
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
    for i in range(2)
)


class ScriptedLLM:
    """Offline ``BaseLLM`` stand-in that answers each pipeline stage in-shape.

    A native turn with no ``tool_calls`` is the terminal signal, so returning
    ``content=<json>, tool_calls=[]`` makes each child finish in one turn with
    its JSON payload as the final text.

    The three child stages want DIFFERENT shapes, so a single canned reply can't
    serve them all — a reviewer's findings-array fed to the filter parses to zero
    valid indices and silently drops every finding. We branch on a marker in the
    stage's system prompt (passed via ``system_msgs``) so each stage gets a
    schema-valid answer:

    * reviewer (``review_one_file``) → a findings array,
    * plan (``make_plan``)          → a ``{"strategy","order"}`` object,
    * filter (``filter_findings``)  → an index array that keeps every finding.
    """

    _FINDINGS = [{"existing_code": "y = 0", "severity": "warning", "message": "suspicious assignment"}]

    def __init__(self, *, model: str = "gpt-4o"):
        self.model = model
        self.cost_manager = None  # router._build sets this if None
        self.calls = 0

    def _reply(self, system_msgs) -> str:
        """Pick the in-shape JSON reply for whichever stage is calling."""
        blob = str(system_msgs or "")
        if "indices to KEEP" in blob:  # review_filter — keep everything (0..N-1; invalid dropped)
            return json.dumps(list(range(64)))
        if "lead of a code review" in blob:  # plan — identity-ish plan
            return json.dumps({"strategy": "", "order": []})
        return json.dumps(self._FINDINGS)  # reviewer (default)

    async def aask_tool(self, msg, system_msgs=None, tools=None, tool_choice=None, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self._reply(system_msgs), tool_calls=[])

    async def aask(self, msg, system_msgs=None, stream=True, **kwargs) -> str:
        self.calls += 1
        return self._reply(system_msgs)

    def format_msg(self, messages):
        return messages


@pytest.fixture
def redirect_sessions(tmp_path, monkeypatch):
    """Keep child rollout logs under tmp (children build a SessionLog)."""
    from pathlib import Path

    import metagpt.session.listing as listing
    import metagpt.session.log as log

    base = Path(tmp_path) / ".agent_sessions"
    monkeypatch.setattr(log, "_default_base_dir", lambda: base)
    monkeypatch.setattr(listing, "_default_base_dir", lambda: base)
    return base


@pytest.fixture
def scripted_llm(monkeypatch):
    """Patch the single LLM factory so every child resolves to the scripted LLM."""
    llm = ScriptedLLM()
    monkeypatch.setattr("metagpt.router.llm.context.create_llm_instance", lambda cfg: llm)
    return llm


@pytest.fixture
def canned_diff(monkeypatch):
    async def fake_get_diff(repo_dir, **kwargs):
        return _DIFF

    monkeypatch.setattr(nodes_mod, "get_diff", fake_get_diff)


async def _run_pipeline(control, repo_dir):
    with set_control(control):
        res = await build_code_review_graph().compile()(
            repo_dir=repo_dir,
            from_ref=None,
            to_ref=None,
            commit=None,
            batch_size=8,
            fmt="text",
            parent_session_id="",
            raw_diff="",
            remaining=[],
            strategy="",
            findings=[],
            kept_findings=None,
            report="",
        )
        assert type(res).__name__ == "BgTaskResult"
        return await res.poll_factory()


@pytest.mark.asyncio
async def test_children_actually_run(tmp_path, redirect_sessions, scripted_llm, canned_diff):
    """The context fix: children are born with a Context and actually invoke the LLM.

    Before the fix, each child crashed with RoleContextNotSetError (swallowed) and
    the LLM was never called. This asserts the real spawn path now reaches the LLM.
    """
    control = AgentControl(store=ResidencyStore(base_dir=str(tmp_path)))
    await _run_pipeline(control, str(tmp_path))
    # 2 reviewer children (one per file) + plan + review_filter also spawn children.
    assert scripted_llm.calls >= 2, f"expected children to invoke the LLM, got {scripted_llm.calls} calls"


@pytest.mark.asyncio
async def test_findings_flow_to_report(tmp_path, redirect_sessions, scripted_llm, canned_diff):
    """End-to-end: the children's JSON findings flow through to the report."""
    control = AgentControl(store=ResidencyStore(base_dir=str(tmp_path)))
    out = await _run_pipeline(control, str(tmp_path))
    report = out.get("report", "")
    findings = out.get("findings", [])
    assert findings, f"expected findings to flow through, got none. report={report!r}"
    assert "suspicious assignment" in report
