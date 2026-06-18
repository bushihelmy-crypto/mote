"""get_node_state — Inspect the per-node execution state of a graph task.

A thin read layer over the authoritative :class:`GraphRunState` recorded by the
bggraph engine and snapshotted onto the task meta. Two modes:

* **Overview** (omit ``nodes``): one status line per node — status, attempts,
  failure reason, route — plus which node is running now. Use it to discover
  which nodes failed / are pending before deciding what to ``resume_tasks``.
* **Detail** (pass ``nodes=[...]``): for the named node(s) only, the node's
  description, declared inputs (name, source, type, description) and output
  (where it lands on the state + which downstream nodes consume it). Drilling
  into specific nodes keeps the output small even for large graphs.
"""
from __future__ import annotations

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.tasks.types import BgStatus

_MSG_UNKNOWN_TASK = "Unknown task_id: {task_id}"
_MSG_NO_RUN_STATE = (
    "Task {task_id} ({command_name}) is status {status}; it has no per-node "
    "state (not a graph pipeline)."
)
_MSG_NODE_NOT_FOUND = "Node '{node_name}' not found in graph. Available: {available}"

_MISSING = object()


def _as_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def _type_name(t) -> str:
    if t is None:
        return ""
    return getattr(t, "__name__", str(t))


def _format_source(source: str) -> str:
    """Render a param's ``from`` declaration in LLM-readable form."""
    if not source:
        return "(unset)"
    if source.startswith("$input."):
        return source  # $input.x — an overridable graph input
    return f"from '{source.split('.')[0]}' output"


def _consumers(graph, node_name: str) -> list[str]:
    """Downstream params that read this node's output, e.g. ['c.in', ...]."""
    out: list[str] = []
    for other_name, other_def in graph._nodes.items():
        if other_name == node_name:
            continue
        for pname, pinfo in other_def.params.items():
            source = pinfo.get("from", "")
            if (
                source
                and not source.startswith("$input.")
                and source.split(".")[0] == node_name
            ):
                out.append(f"{other_name}.{pname}")
    return out


def _record_header(rec) -> str:
    line = rec.status.value
    if rec.attempts:
        line += f" (attempts {rec.attempts})"
    if rec.status == BgStatus.FAILED and rec.last_error:
        line += f" — error: {rec.last_error}"
    if rec.last_route_key:
        line += f" — route: {rec.last_route_key}"
    return line


@register_tool
class GetNodeState(BaseTool):
    name = "GetNodeState"
    aliases = ["get_node_state"]
    description = (
        "Inspect the per-node execution state of a background graph pipeline. "
        "Omit 'nodes' for an overview: every node's status "
        "(success/failed/pending/skipped/running), attempt count and failure "
        "reason, plus which node is running now. Pass nodes=[...] to drill into "
        "specific node(s): their description, declared inputs (name/source/type) "
        "and output (and which downstream nodes consume it). Use it before "
        "resume_tasks to decide which nodes to re-run, skip, or override."
    )
    requires = ("get_bg_pool",)

    async def call(self, *, task_id: str, nodes: str | list[str] | None = None) -> str:
        """Report the per-node state of a background graph task.

        Args:
            task_id: The task ID to inspect (e.g. "bg_3").
            nodes: Optional node name(s) to drill into. Omit for a status
                overview of all nodes; pass a name or list of names to get those
                nodes' description, inputs and output only.
        """
        pool = self.get_bg_pool()
        meta = pool.get_task_info(task_id)
        if meta is None:
            raise ToolError(_MSG_UNKNOWN_TASK.format(task_id=task_id))

        run_state = pool.get_run_state(task_id)
        if run_state is None:
            return _MSG_NO_RUN_STATE.format(
                task_id=task_id,
                command_name=meta.command_name,
                status=meta.status.value,
            )

        requested = _as_list(nodes)
        if not requested:
            return self._render_overview(task_id, meta, run_state)

        gm = meta.graph_meta
        graph = gm.graph_ref if gm else None
        if graph is not None:
            for n in requested:
                if n not in graph._nodes:
                    raise ToolError(
                        _MSG_NODE_NOT_FOUND.format(
                            node_name=n, available=list(graph._nodes.keys())
                        )
                    )
        return self._render_details(task_id, meta, run_state, graph, requested)

    def _render_overview(self, task_id, meta, run_state) -> str:
        lines = [
            f"Task {task_id} ({meta.command_name}) — status: {meta.status.value}",
            "nodes:",
        ]
        for rec in run_state.records.values():
            lines.append(f"  - {rec.name}: {_record_header(rec)}")
        running = run_state.running_names()
        lines.append(f"running: {', '.join(running) if running else '(none)'}")
        lines.append(
            "tip: call GetNodeState with nodes=[...] to see a node's "
            "description, inputs and output."
        )
        return "\n".join(lines)

    def _render_details(self, task_id, meta, run_state, graph, requested) -> str:
        state = meta.state_snapshot
        blocks = [f"Task {task_id} ({meta.command_name}) — status: {meta.status.value}"]
        for n in requested:
            rec = run_state.get(n)
            block = [f"Node '{n}': {_record_header(rec)}"]
            node_def = graph._nodes.get(n) if graph is not None else None
            if node_def is None:
                blocks.append("\n".join(block))
                continue

            if node_def.description:
                block.append(f"  description: {node_def.description}")

            if node_def.params:
                block.append("  inputs:")
                for pname, pinfo in node_def.params.items():
                    seg = f"    - {pname}: {_format_source(pinfo.get('from', ''))}"
                    tp = _type_name(pinfo.get("type"))
                    if tp:
                        seg += f" [{tp}]"
                    desc = pinfo.get("desc", "")
                    if desc:
                        seg += f" — {desc}"
                    block.append(seg)
            else:
                block.append("  inputs: (none declared)")

            out_line = f"  output: state.{n}"
            if state is not None:
                val = getattr(state, n, _MISSING)
                if val is not _MISSING:
                    out_line += f" [{type(val).__name__}]"
            consumers = _consumers(graph, n)
            if consumers:
                out_line += f" — consumed by: {', '.join(consumers)}"
            block.append(out_line)
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)
