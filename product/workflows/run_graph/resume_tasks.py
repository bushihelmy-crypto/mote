"""resume_tasks — Resume or restart durable Workflow runs.

Covers two scenarios:
1. Error recovery: resume from a failed node, skip a node, or restart with new params.
2. LLM routing: when a graph pauses at an LLM edge, resume from the chosen node.

The tool operates on run IDs returned by the durable Workflow service. It
supports per-node resume (from_node), per-node skip (skip_node — bypasses only
the named node while the rest of the graph keeps running downstream), and
parameter overrides (**kwargs applied to the graph state).
"""

from __future__ import annotations

from typing import Any

from mote.contracts.tool.errors import ToolError
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.orchestration.workflows.durable import WorkflowRunPhase
from mote.product.workflows.agent_context import resolve_agent_workflows
from mote.product.workflows.agent_service import WorkflowResumePlan
from mote.runtime.tools.base_tool import BaseTool

# Messages aligned with the design doc (§9)
_MSG_UNKNOWN_RUN = "Unknown Workflow run_id: {run_id}"
_MSG_RUN_ALREADY_DONE = "Workflow run {run_id} is already {status}, cannot resume."
_MSG_RESUMED = "Workflow run {run_id} resumed."
_MSG_NODE_NOT_FOUND = "Node '{node_name}' not found in graph. Available: {available}"
_MSG_INFEASIBLE = (
    "Cannot resume from {detail}. Re-run the upstream node(s) too "
    "(add them to from_node), or skip them (skip_node) if their output is not needed."
)
_MSG_UNKNOWN_OVERRIDE = (
    "Unknown override key(s): {keys}. Valid input fields for this task: {valid}. "
    "Overrides set graph input fields only — node outputs are recomputed on re-run, "
    "not overridable."
)


def _as_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def _missing_upstreams(
    graph: Any, from_nodes: list[str], skip_nodes: list[str], completed: set
) -> dict[str, list[str]]:
    """Per re-run node, the declared param-source nodes that aren't satisfied.

    A node's ``params`` declare ``from`` sources (``$input.x`` or
    ``upstream.key``). Re-running a node needs each non-input upstream to be
    completed (or itself being re-run / skipped in this same call); otherwise the
    node would execute with missing inputs. Returns ``{node: [missing, ...]}``.
    """
    being_handled = set(from_nodes) | set(skip_nodes)
    missing: dict[str, list[str]] = {}
    for fn in from_nodes:
        node_def = graph._nodes.get(fn)
        if node_def is None:
            continue
        gaps: set[str] = set()
        for pinfo in node_def.params.values():
            source = pinfo.get("from", "")
            if not source or source.startswith("$input."):
                continue
            upstream = source.split(".")[0]
            if upstream in being_handled or upstream in completed:
                continue
            gaps.add(upstream)
        if gaps:
            missing[fn] = sorted(gaps)
    return missing


class ResumeTasks(BaseTool):
    name = "ResumeTasks"

    async def call(
        self,
        *,
        run_id: str,
        definition_id: str,
        from_node: str | list[str] | None = None,
        skip_node: str | list[str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> str:
        """Resume or restart a paused/failed pipeline — re-run, skip, or override nodes.

        Resume or restart a paused/failed durable Workflow run. Use
        ``from_node`` to re-run specific nodes; use ``skip_node`` to skip ONLY the
        named node(s) while the rest of the pipeline keeps running its downstream
        nodes to completion (skip does NOT stop or cancel the pipeline — it marks
        just those nodes done/skipped and the DAG continues). Omit both
        ``from_node`` and ``skip_node`` to restart the whole pipeline from
        scratch. Use ``overrides`` to change input parameters before resuming
        (keys come from the 'params' section shown in failure/routing
        notifications).

        Args:
            run_id: The durable Workflow run ID to resume.
            definition_id: The immutable Workflow definition identity returned with the run.
            from_node: Node name(s) to re-run. Omit to restart the entire pipeline.
            skip_node: Node name(s) to skip. The skipped node is marked done (keeping
                any partial result, otherwise an empty result) and is NOT executed.
                The rest of the graph keeps running — its downstream nodes execute
                normally and the pipeline runs to completion. This does not stop or
                cancel the task; it only bypasses the named node(s).
                Can be combined with from_node (skip some, re-run others).
            overrides: Key-value pairs to override on the graph state before resuming.
                Keys must match fields declared in the node's params (shown in failure
                notifications). Example: {"prompt": "new prompt", "style": "cinematic"}.
        """
        workflows = resolve_agent_workflows()
        reference = WorkflowRunReference(WorkflowRunId(run_id), WorkflowDefinitionId(definition_id))
        meta = workflows.view(reference)
        if meta is None:
            raise ToolError(_MSG_UNKNOWN_RUN.format(run_id=run_id))

        # Cannot resume a completed or currently running task
        if meta.status in {
            WorkflowRunPhase.RUNNING,
            WorkflowRunPhase.SUCCEEDED,
            WorkflowRunPhase.FAILED,
            WorkflowRunPhase.CANCELLED,
            WorkflowRunPhase.TIMED_OUT,
        }:
            return _MSG_RUN_ALREADY_DONE.format(run_id=run_id, status=meta.status.value)

        from_nodes = _as_list(from_node)
        skip_nodes = _as_list(skip_node)

        try:
            await workflows.resume_plan(
                reference,
                WorkflowResumePlan(tuple(from_nodes), tuple(skip_nodes), overrides),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

        return _MSG_RESUMED.format(run_id=run_id)
