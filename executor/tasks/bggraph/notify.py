"""Notification rendering for bggraph (ported from design v5 §7).

All notifications are pushed through :func:`report_progress` so they land in the
task's disk output and surface to the LLM via the existing
``TaskAttachmentGenerator`` as ``<delta-summary>`` blocks.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from metagpt.executor.tasks.bggraph.report import _truncate, report_progress
from metagpt.executor.tasks.bggraph.types import (
    END,
    GraphBatchFailureError,
    BgStatus,
)

# ---------------------------------------------------------------------------
# Per-node progress message templates
# ---------------------------------------------------------------------------

_MSG_RETRYING = "retrying ({attempt}/{auto_retries}): {error_type}: {error}"
_MSG_FAILED = "{error_type}: {error}"
_MSG_FAILED_WITH_RETRY = "{error_type}: {error} (retried {attempt}/{auto_retries}, all failed)"
_MSG_COMPLETED_WITH_RETRY = "{result} (after {auto_retries} retry)"

# --- task lifecycle messages ---
_MSG_RESUMING = "Resuming from node '{from_node}'"
_MSG_SKIPPING = "Skipping nodes [{skip_nodes}], continuing downstream{suffix}"
_MSG_RESUMED = "Task {task_id} resumed"
_MSG_MAX_RESTARTS = "max_restarts exceeded ({retry_count}/{max_restarts})"
_MSG_UNKNOWN_TASK = "Unknown task_id: {task_id}"
_MSG_TASK_ALREADY_DONE = "Task {task_id} is already {status}, cannot resume."
_MSG_NODE_NOT_RESUMABLE = (
    "Node '{node_name}' is {node_status}, cannot resume. "
    "Only failed or not-yet-run nodes can be resumed."
)
_MSG_CANCEL_DONE = "Task {task_id} is already {status}, cannot cancel."
_MSG_CANCELLED_REASON = "Cancelled: {reason}"
_MSG_CANCEL_SUCCESS = (
    "Task {task_id} ({command_name}) cancelled.\n"
    "Completed nodes:\n{completed_nodes_text}\n"
    "Reason: {reason}\n"
    "Use resume_tasks(task_id='{task_id}', from_node='...') to resume."
)

# ---------------------------------------------------------------------------
# Notification rendering templates
# ---------------------------------------------------------------------------

_FMT_NOTIFICATION_STARTED = (
    '"{command}" task started (task_id: {task_id})\n'
    "stage-summary:\n{stage_summary}"
)
_FMT_NOTIFICATION_SUCCESS = (
    '"{command}" task success (task_id: {task_id})\n'
    "result: {result}"
)
_FMT_NOTIFICATION_FAILED = (
    '"{command}" task failed (task_id: {task_id})\n'
    "stage-summary:\n{stage_summary}\n"
    "DAG paused, all nodes finished.\n"
    "task params: {initial_params}\n"
    "failed nodes:\n{failed_nodes_text}\n"
    "waiting_for_route nodes:\n{waiting_nodes_text}\n"
    "completed nodes:\n{completed_nodes_text}\n"
    "skipped nodes:\n{skipped_nodes_text}\n"
    "pending nodes:\n{pending_nodes_text}"
)
_FMT_NODE_FAILURE_NOTIFICATION = (
    '"{command}" task node_failed (task_id: {task_id})\n'
    "stage-summary:\n{stage_summary}\n"
    "failed node:\n{failed_node_text}\n"
    "waiting_for_route nodes:\n{waiting_nodes_text}\n"
    "running nodes:\n{running_nodes_text}\n"
    "completed nodes:\n{completed_nodes_text}\n"
    "skipped nodes:\n{skipped_nodes_text}\n"
    "pending nodes:\n{pending_nodes_text}\n"
    "{action_hint}"
)
# Trailing action hint for node_failed — driven by whether any node is still
# running. No running node means the graph has stalled and a terminal failure
# follows immediately, so a decision is needed now.
_HINT_NODE_FAILURE_STALLED = "无节点可运行，请立即做出决策或向用户询问。"
_HINT_NODE_FAILURE_RUNNING = "仍有节点可运行，可稍后再做决策。"
_FMT_LLM_ROUTE_NOTIFICATION = (
    '"{command}" task waiting_for_route (task_id: {task_id})\n'
    "stage-summary:\n{stage_summary}\n"
    "current node:\n"
    "  - {from_node}\n"
    "    description: {from_node_desc}\n"
    "    result: {from_node_result}\n"
    "{prompt}\n"
    "options:\n{options_text}\n"
    "{action_hint}"
)
_FMT_LLM_ROUTE_OPTION = (
    '  - resume_tasks(from_node="{target_node}")  → {route_key}\n'
    "    description: {target_desc}\n"
    "    params:\n{target_params_text}"
)
_HINT_OPTIONAL = "You may also do nothing (route to END requires no action)."
_HINT_REQUIRED = "You MUST choose one of the above options to continue."

_FMT_FAILED_NODE_BLOCK = (
    "  - {node_name}\n"
    "    description: {node_desc}\n"
    "    error: {error}\n"
    "    auto_retries: {auto_retries_text}\n"
    "    params:\n{node_params_text}"
)
_FMT_COMPLETED_NODE_BLOCK = (
    "  - {node_name}\n"
    "    description: {node_desc}\n"
    "    result: {result}"
)
_FMT_NODE_PARAM = "      {name} = {value}\n        {desc}, {source}"
_FMT_WAITING_NODE_BLOCK = (
    "  - {node_name}\n"
    "    description: {node_desc}\n"
    "    prompt: {prompt}"
)
# Bare name + description block, shared by running / skipped / pending sections
# (these report node identity only — no result, error or prompt).
_FMT_SIMPLE_NODE_BLOCK = "  - {node_name}\n    description: {node_desc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_auto_retries(exc: BaseException) -> str:
    attempted = getattr(exc, "_auto_retries_attempted", 0)
    limit = getattr(exc, "_auto_retries_limit", 0)
    if limit == 0:
        return "0/0 (no auto-retry)"
    return f"{attempted}/{limit} (all failed)"


def _resolve_param_source(source: str, state: Any, initial_params: dict) -> Any:
    """Resolve a ``params['from']`` source against the state / initial params."""
    if source.startswith("$input."):
        field_name = source[len("$input."):]
        if field_name in initial_params:
            return initial_params.get(field_name)
        return getattr(state, field_name, None)
    parts = source.split(".", 1)
    node_name = parts[0]
    node_output = getattr(state, node_name, None)
    if len(parts) == 1:
        return node_output
    key = parts[1]
    if isinstance(node_output, dict):
        return node_output.get(key)
    return getattr(node_output, key, None)


def _render_node_params(node_name: str, graph: Any, state: Any) -> str:
    node_def = graph._nodes.get(node_name)
    if not node_def or not node_def.params:
        return "      (none)"
    lines = []
    for name, info in node_def.params.items():
        source = info["from"]
        source_text = (
            "task input param"
            if source.startswith("$input.")
            else f"from {source.split('.')[0]} node output"
        )
        value = _resolve_param_source(source, state, {})
        lines.append(
            _FMT_NODE_PARAM.format(
                name=name, value=repr(value), desc=info.get("desc", ""), source=source_text
            )
        )
    return "\n".join(lines) if lines else "      (none)"


def _waiting_names(graph: Any, completed: set) -> set:
    """Names of completed nodes parked on an LLM-route edge (waiting_for_route).

    These take precedence over the ``completed nodes`` section so a node is
    reported in exactly one place.
    """
    return {
        le.from_node
        for le in graph._llm_edges
        if le.from_node in completed and le.from_node in graph._nodes
    }


def _render_waiting_nodes(graph: Any, completed: set) -> str:
    blocks = []
    for llm_edge in graph._llm_edges:
        if llm_edge.from_node in completed and llm_edge.from_node in graph._nodes:
            nd = graph._nodes[llm_edge.from_node]
            blocks.append(
                _FMT_WAITING_NODE_BLOCK.format(
                    node_name=llm_edge.from_node, node_desc=nd.description, prompt=llm_edge.prompt
                )
            )
    return "\n".join(blocks) if blocks else "  (none)"


def _render_completed_nodes(
    graph: Any, state: Any, completed: Optional[set] = None, failed_names: Optional[set] = None
) -> str:
    """Render SUCCESS nodes (with a result). SKIPPED nodes go to their own
    section; nodes parked on an LLM route are reported under waiting_for_route,
    and nodes present in *failed_names* are reported under failed — both are
    excluded here so each node appears in exactly one section. The failed
    exclusion matters under cyclic graphs, where a node may fail and then
    succeed on a later loop (SUCCESS status while still in the failure list).
    """
    blocks = []
    names = completed if completed is not None else list(graph._nodes.keys())
    excluded = _waiting_names(graph, set(names)) | (failed_names or set())
    for name in names:
        if name not in graph._nodes or name in excluded:
            continue
        nd = graph._nodes[name]
        if nd.status != BgStatus.SUCCESS:
            continue
        result = getattr(state, name, None)
        if result is None:
            continue
        blocks.append(
            _FMT_COMPLETED_NODE_BLOCK.format(
                node_name=name, node_desc=nd.description, result=_truncate(result)
            )
        )
    return "\n".join(blocks) if blocks else "  (none)"


def _render_status_nodes(graph: Any, status: BgStatus) -> str:
    """Render nodes whose status equals *status* as bare name+description blocks.

    Used for the ``skipped`` and ``pending`` sections — these report node
    identity only, without result/error/prompt detail.
    """
    blocks = [
        _FMT_SIMPLE_NODE_BLOCK.format(node_name=name, node_desc=nd.description)
        for name, nd in graph._nodes.items()
        if nd.status == status
    ]
    return "\n".join(blocks) if blocks else "  (none)"


def _dedup_failures(failures: list) -> list:
    """Collapse repeated failures of the same node (cyclic re-runs) into one
    entry, keeping the last failure (final retry counts) in first-seen order.
    """
    seen: dict = {}
    for name, exc in failures:
        seen[name] = exc  # last-wins; reassigning a key preserves its position
    return list(seen.items())


def _render_failed_nodes(error: Optional[BaseException], graph: Any, state: Any) -> str:
    failures = error.failures if isinstance(error, GraphBatchFailureError) else []
    blocks = []
    for name, exc in _dedup_failures(failures):
        if name in graph._nodes:
            nd = graph._nodes[name]
            blocks.append(
                _FMT_FAILED_NODE_BLOCK.format(
                    node_name=name,
                    node_desc=nd.description,
                    error=_truncate(exc),
                    auto_retries_text=_format_auto_retries(exc),
                    node_params_text=_render_node_params(name, graph, state),
                )
            )
    return "\n".join(blocks) if blocks else "  (none)"


def _render_running_nodes(running_names: list[str], graph: Any) -> str:
    blocks = [
        _FMT_SIMPLE_NODE_BLOCK.format(node_name=name, node_desc=graph._nodes[name].description)
        for name in running_names
        if name in graph._nodes
    ]
    return "\n".join(blocks) if blocks else "  (none)"


# ---------------------------------------------------------------------------
# Structured notification model
# ---------------------------------------------------------------------------


class TaskNotification(BaseModel):
    """Structured task notification for LLM consumption / internal passing."""

    task_id: str
    command: str
    status: BgStatus
    event: str  # "started" | "node_failed" | "terminal"
    result: Optional[str] = None
    initial_params: Optional[dict] = None
    stage_summary: str = ""
    failed_nodes_text: str = "  (none)"
    completed_nodes_text: str = "  (none)"
    waiting_nodes_text: str = "  (none)"
    skipped_nodes_text: str = "  (none)"
    pending_nodes_text: str = "  (none)"

    def to_text(self) -> str:
        if self.status == BgStatus.FAILED:
            return _FMT_NOTIFICATION_FAILED.format(
                command=self.command,
                task_id=self.task_id,
                stage_summary=self.stage_summary,
                initial_params=self.initial_params or {},
                failed_nodes_text=self.failed_nodes_text,
                completed_nodes_text=self.completed_nodes_text,
                waiting_nodes_text=self.waiting_nodes_text,
                skipped_nodes_text=self.skipped_nodes_text,
                pending_nodes_text=self.pending_nodes_text,
            )
        return _FMT_NOTIFICATION_SUCCESS.format(
            command=self.command,
            task_id=self.task_id,
            result=_truncate(self.result) if self.result else "",
        )


# ---------------------------------------------------------------------------
# Push functions (all route through report_progress)
# ---------------------------------------------------------------------------


def push_started_notification(graph: Any, task_id: str = "(current)") -> None:
    text = _FMT_NOTIFICATION_STARTED.format(
        command=graph.command_name,
        task_id=task_id,
        stage_summary=graph._build_stage_summary(),
    )
    report_progress(END, BgStatus.RUNNING, text)


def push_terminal_notification(
    graph: Any,
    state: Any,
    status: BgStatus,
    *,
    result: Any = None,
    error: Optional[BaseException] = None,
    initial_params: Optional[dict] = None,
    task_id: str = "(current)",
) -> None:
    failures = error.failures if isinstance(error, GraphBatchFailureError) else []
    failed_names = {name for name, _ in failures}
    tn = TaskNotification(
        task_id=task_id,
        command=graph.command_name,
        status=status,
        event="terminal",
        result=_truncate(result) if result else None,
        initial_params=initial_params,
        stage_summary=graph._build_stage_summary(),
        failed_nodes_text=_render_failed_nodes(error, graph, state),
        # Exclude failed nodes so a cyclic fail→succeed node is not double-listed.
        completed_nodes_text=_render_completed_nodes(graph, state, failed_names=failed_names),
        # The graph is never paused on an LLM route at terminal → no waiting node.
        waiting_nodes_text=_render_waiting_nodes(graph, set()),
        skipped_nodes_text=_render_status_nodes(graph, BgStatus.SKIPPED),
        pending_nodes_text=_render_status_nodes(graph, BgStatus.PENDING),
    )
    report_progress(END, status, tn.to_text())


def push_node_failure_notification(
    failed_name: str,
    exc: BaseException,
    state: Any,
    graph: Any,
    *,
    completed: set,
    running_names: list[str],
    task_id: str = "(current)",
) -> None:
    # node_failed is a discrete event note: it reports only the node that just
    # failed. The cumulative (de-duplicated) failure picture is reconciled once,
    # in the terminal notification — so this intermediate note carries no
    # historical failure state (which would also double-report under cycles).
    node_def = graph._nodes[failed_name]
    failed_text = _FMT_FAILED_NODE_BLOCK.format(
        node_name=failed_name,
        node_desc=node_def.description,
        error=_truncate(exc),
        auto_retries_text=_format_auto_retries(exc),
        node_params_text=_render_node_params(failed_name, graph, state),
    )
    notification = _FMT_NODE_FAILURE_NOTIFICATION.format(
        command=graph.command_name,
        task_id=task_id,
        stage_summary=graph._build_stage_summary(),
        failed_node_text=failed_text,
        completed_nodes_text=_render_completed_nodes(graph, state, completed),
        running_nodes_text=_render_running_nodes(running_names, graph),
        waiting_nodes_text=_render_waiting_nodes(graph, completed),
        skipped_nodes_text=_render_status_nodes(graph, BgStatus.SKIPPED),
        pending_nodes_text=_render_status_nodes(graph, BgStatus.PENDING),
        action_hint=_HINT_NODE_FAILURE_RUNNING if running_names else _HINT_NODE_FAILURE_STALLED,
    )
    report_progress(failed_name, BgStatus.FAILED, notification)


def push_llm_route_notification(llm_edge: Any, state: Any, graph: Any, task_id: str = "(current)") -> None:
    from_node_def = graph._nodes.get(llm_edge.from_node)
    from_node_desc = from_node_def.description if from_node_def else ""
    from_node_result = _truncate(getattr(state, llm_edge.from_node, None))

    has_end = END in llm_edge.mapping.values()
    options = []
    for route_key, target_node in llm_edge.mapping.items():
        if target_node == END:
            continue
        target_def = graph._nodes.get(target_node)
        target_desc = target_def.description if target_def else ""
        options.append(
            _FMT_LLM_ROUTE_OPTION.format(
                target_node=target_node,
                route_key=route_key,
                target_desc=target_desc,
                target_params_text=_render_node_params(target_node, graph, state),
            )
        )
    options_text = "\n".join(options) if options else "  (none)"
    action_hint = _HINT_OPTIONAL if has_end else _HINT_REQUIRED

    notification = _FMT_LLM_ROUTE_NOTIFICATION.format(
        command=graph.command_name,
        task_id=task_id,
        stage_summary=graph._build_stage_summary(),
        from_node=llm_edge.from_node,
        from_node_desc=from_node_desc,
        from_node_result=from_node_result,
        prompt=llm_edge.prompt,
        options_text=options_text,
        action_hint=action_hint,
    )
    report_progress(llm_edge.from_node, BgStatus.WAITING_FOR_ROUTE, notification)
