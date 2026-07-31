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

import json

from mote.orchestration.background_tasks.model import BgStatus
from mote.runtime.errors import ToolError
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import GetBgPool
from mote.runtime.tools.text_normalization import collapse_whitespace
from mote.runtime.tools.tool_registry import register_tool

_MSG_UNKNOWN_TASK = "Unknown task_id: {task_id}"
_MSG_NO_RUN_STATE = (
    "Task {task_id} ({command_name}) is status {status}; it has no per-node " "state (not a graph pipeline)."
)
_MSG_NODE_NOT_FOUND = "Node '{node_name}' not found in graph. Available: {available}"
_MSG_UNKNOWN_FIELD = "Unknown state field(s): {fields}. Available: {available}"
_MSG_NO_SNAPSHOT = "Task {task_id} has no state snapshot to read fields from."
_MSG_LIST_AS_STRING = (
    "Expected a list, got a JSON string: {value}. Pass a real array "
    '(e.g. ["findings", "report"]), not a stringified one.'
)

# Inline previews stay short (the overview/detail view is a compact one-line
# glance across many nodes); an explicit ``fields=`` dump returns the FULL value
# so the model can recover a produced value (e.g. the report / findings) that a
# notification cut — a large dump is persisted to disk by the shared tool-result
# exit rather than truncated here.
_INLINE_PREVIEW_LIMIT = 200

# Sentinel distinguishing "field absent" from a legitimate ``None`` value.
_NO_FIELD = object()


def _as_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, str):
        # A single bare name is fine; a JSON-array-looking string means the
        # model serialized a list arg instead of passing a real list. Reject it
        # with a clear message rather than treating the whole "[...]" blob as one
        # (bogus) field name.
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            raise ToolError(_MSG_LIST_AS_STRING.format(value=val))
        return [val]
    return list(val)


def _type_name(t) -> str:
    if t is None:
        return ""
    return getattr(t, "__name__", str(t))


def _source_field(source: str) -> str | None:
    """The state field a ``from`` source reads, or ``None`` when unset.

    Both ``$input.<field>`` (graph input) and ``<field>`` / ``<field>.<key>``
    (upstream-written state field) resolve to a state field on the snapshot.
    """
    if not source:
        return None
    if source.startswith("$input."):
        return source[len("$input.") :]
    return source.split(".")[0]


def _format_source(source: str) -> str:
    """Render a param's ``from`` declaration in LLM-readable form."""
    if not source:
        return "(unset)"
    if source.startswith("$input."):
        return f"graph input '{source[len('$input.') :]}'"  # overridable via resume overrides
    return f"state field '{source.split('.')[0]}'"


def _preview_value(value, *, limit: int | None = None, collapse: bool = False) -> str:
    """Render a state value. Pass *limit* to cap an inline one-line preview.

    Lists/tuples are prefixed with their length so the model sees the shape even
    when the body is capped. *collapse* squashes newlines (for inline one-line
    previews). With *limit* unset the FULL value is returned (the ``fields=``
    dump path) — a large dump is persisted to disk by the shared tool-result
    exit rather than truncated here.
    """
    if value is None:
        return "None"
    prefix = f"[{len(value)} items] " if isinstance(value, (list, tuple)) else ""
    if isinstance(value, str):
        body = value
    else:
        try:
            body = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            body = repr(value)
    body = body.strip()
    if collapse:
        body = collapse_whitespace(body)
    if limit is not None and len(body) > limit:
        body = f"{body[:limit]}… (+{len(body) - limit} more chars)"
    return f"{prefix}{body}"


def _consumers(graph, written_fields: list[str]) -> list[str]:
    """Downstream params that read any of *written_fields*, e.g. ['c.in', ...].

    A node writes state fields (the keys of its update dict, recorded on the run
    record); a consumer is any other node whose param ``from`` resolves to one of
    those fields. This works regardless of whether a node writes a field named
    after itself or a differently-named channel (e.g. ``load_diff`` → ``raw_diff``).
    """
    fields = set(written_fields)
    if not fields:
        return []
    out: list[str] = []
    for other_name, other_def in graph._nodes.items():
        for pname, pinfo in other_def.params.items():
            source = pinfo.get("from", "")
            if not source or source.startswith("$input."):
                continue
            if source.split(".")[0] in fields:
                out.append(f"{other_name}.{pname}")
    return out


def _is_self_loop_node(graph, name: str) -> bool:
    """Whether *name* is a ring node (routes back to itself); delegates to graph."""
    if graph is None:
        return False
    return graph.is_self_loop(name)


def _record_header(rec, *, self_loop: bool = False) -> str:
    line = rec.status.value
    if rec.attempts:
        if self_loop:
            # Ring node: attempts == laps around the loop, not failed retries.
            line += f" ({rec.attempts} activations — laps, not retries)"
        else:
            line += f" (attempts {rec.attempts})"
    if rec.status == BgStatus.FAILED and rec.last_error:
        line += f" — error: {rec.last_error}"
    if rec.last_route_key:
        line += f" — route: {rec.last_route_key}"
    return line


@register_tool
class GetNodeState(BaseTool):
    name = "GetNodeStates"
    aliases = ["get_node_state"]
    requires = ("get_bg_pool",)

    # Injected from Role by bind(): Role.get_bg_pool.
    get_bg_pool: GetBgPool

    async def call(
        self,
        *,
        task_id: str,
        nodes: str | list[str] | None = None,
        fields: str | list[str] | None = None,
    ) -> str:
        """Inspect a background pipeline's per-node state — diagnose a failed or paused task.

        Inspect the per-node execution state of a background graph pipeline.
        DIAGNOSTIC only — reach for it when a task FAILED/TIMED OUT/paused or its
        result looks wrong. Do NOT poll a running task: it pushes a completion
        notification with the full result, so just Sleep to wait.

        Omit all args for an overview: every node's status
        (success/failed/pending/skipped/running), attempt count and failure
        reason, plus which node is running now. Pass ``nodes=[...]`` to drill into
        specific node(s): their description, declared inputs (source/type + a
        preview of the current value) and outputs (the state fields the node
        wrote + a preview, and which downstream nodes consume them). Pass
        ``fields=[...]`` to dump the full current value of specific state fields
        (e.g. findings, report, raw_diff). Use it before resume_tasks to
        decide which nodes to re-run, skip, or override.

        Args:
            task_id: The task ID to inspect (e.g. "bg_3").
            nodes: Optional node name(s) to drill into. Omit for a status
                overview of all nodes; pass a name or list of names to get those
                nodes' description, inputs and outputs only.
            fields: Optional state field name(s) to dump the full current value
                of (e.g. ["findings", "report"]). Reads the task's state
                snapshot; takes precedence over ``nodes``.
        """
        pool = self.get_bg_pool()
        meta = pool.get_task_info(task_id)
        if meta is None:
            raise ToolError(_MSG_UNKNOWN_TASK.format(task_id=task_id))

        wanted_fields = _as_list(fields)
        if wanted_fields:
            out = self._render_fields(task_id, meta, wanted_fields)
            pool.mark_retrieved(task_id)
            return out

        run_state = pool.get_run_state(task_id)
        if run_state is None:
            return _MSG_NO_RUN_STATE.format(
                task_id=task_id,
                command_name=meta.command_name,
                status=meta.status.value,
            )

        gm = meta.graph_meta
        graph = gm.graph_ref if gm else None

        requested = _as_list(nodes)
        if not requested:
            out = self._render_overview(task_id, meta, run_state, graph)
            pool.mark_retrieved(task_id)
            return out

        if graph is not None:
            for n in requested:
                if n not in graph._nodes:
                    raise ToolError(_MSG_NODE_NOT_FOUND.format(node_name=n, available=list(graph._nodes.keys())))
        out = self._render_details(task_id, meta, run_state, graph, requested)
        pool.mark_retrieved(task_id)
        return out

    def _render_overview(self, task_id, meta, run_state, graph=None) -> str:
        lines = [
            f"Task {task_id} ({meta.command_name}) — status: {meta.status.value}",
            "nodes:",
        ]
        for rec in run_state.records.values():
            self_loop = _is_self_loop_node(graph, rec.name)
            lines.append(f"  - {rec.name}: {_record_header(rec, self_loop=self_loop)}")
        running = run_state.running_names()
        lines.append(f"running: {', '.join(running) if running else '(none)'}")
        lines.append(
            "tip: call GetNodeState with nodes=[...] for a node's inputs/outputs, "
            "or fields=[...] to dump a state field's full value."
        )
        return "\n".join(lines)

    def _render_details(self, task_id, meta, run_state, graph, requested) -> str:
        state = meta.state_snapshot
        blocks = [f"Task {task_id} ({meta.command_name}) — status: {meta.status.value}"]
        for n in requested:
            rec = run_state.get(n)
            self_loop = _is_self_loop_node(graph, n)
            block = [f"Node '{n}': {_record_header(rec, self_loop=self_loop)}"]
            node_def = graph._nodes.get(n) if graph is not None else None
            if node_def is None:
                blocks.append("\n".join(block))
                continue

            if node_def.description:
                block.append(f"  description: {node_def.description}")

            self._render_inputs(block, node_def, state)
            self._render_outputs(block, graph, rec, state)
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)

    def _render_inputs(self, block, node_def, state) -> None:
        """Append each declared input's source/type/desc + a value preview."""
        if not node_def.params:
            block.append("  inputs: (none declared)")
            return
        block.append("  inputs:")
        for pname, pinfo in node_def.params.items():
            seg = f"    - {pname}: {_format_source(pinfo.get('from', ''))}"
            tp = _type_name(pinfo.get("type"))
            if tp:
                seg += f" [{tp}]"
            desc = pinfo.get("desc", "")
            if desc:
                seg += f" — {desc}"
            field = _source_field(pinfo.get("from", ""))
            preview = self._field_preview(state, field)
            if preview is not None:
                seg += f" = {preview}"
            block.append(seg)

    def _render_outputs(self, block, graph, rec, state) -> None:
        """Append the fields this node wrote (with previews) + their consumers."""
        writes = list(rec.writes or [])
        if not writes:
            # No recorded writes: the node has not completed (pending/failed) or
            # produced no state update. Say so rather than assert an output.
            block.append("  output: (no state fields recorded yet — node not completed)")
            return
        block.append(f"  output: writes {', '.join(writes)}")
        for field in writes:
            preview = self._field_preview(state, field)
            if preview is not None:
                block.append(f"    - {field} = {preview}")
        consumers = _consumers(graph, writes) if graph is not None else []
        if consumers:
            block.append(f"  consumed by: {', '.join(consumers)}")

    @staticmethod
    def _field_preview(state, field):
        """Short inline preview of ``state.field``, or ``None`` when unavailable."""
        if state is None or not field:
            return None
        value = getattr(state, field, _NO_FIELD)
        if value is _NO_FIELD:
            return None
        return _preview_value(value, limit=_INLINE_PREVIEW_LIMIT, collapse=True)

    def _render_fields(self, task_id, meta, requested) -> str:
        """Dump the full current value of the requested state snapshot fields."""
        state = meta.state_snapshot
        if state is None:
            return _MSG_NO_SNAPSHOT.format(task_id=task_id)
        valid = set(getattr(type(state), "model_fields", {}) or {})
        unknown = [f for f in requested if f not in valid and getattr(state, f, _NO_FIELD) is _NO_FIELD]
        if unknown:
            raise ToolError(
                _MSG_UNKNOWN_FIELD.format(
                    fields=", ".join(unknown),
                    available=", ".join(sorted(valid)) or "(none declared)",
                )
            )
        blocks = [f"Task {task_id} ({meta.command_name}) — state fields:"]
        for field in requested:
            value = getattr(state, field, _NO_FIELD)
            if value is _NO_FIELD:
                blocks.append(f"{field}: (not set)")
                continue
            blocks.append(f"{field}:\n{_preview_value(value)}")
        return "\n\n".join(blocks)
