"""Notification rendering for bggraph (ported from design v5 §7).

All notifications are pushed through :func:`report_progress` so they land in the
task's disk output and surface to the LLM via the existing
``TaskAttachmentGenerator`` as ``<delta-summary>`` blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from mote.contracts.foundation.errors.report import ErrorReport, render_error_block
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId
from mote.orchestration.workflows.events import _as_text, emit_workflow_progress
from mote.orchestration.workflows.types import END, GraphRunState, WorkflowNodeStatus

# ---------------------------------------------------------------------------
# Per-node progress message templates
# ---------------------------------------------------------------------------

_MSG_RETRYING = "retrying ({attempt}/{auto_retries}): {error_type}: {error}"
_MSG_FAILED = "{error_type}: {error}"
_MSG_FAILED_WITH_RETRY = "{error_type}: {error} (retried {attempt}/{auto_retries}, all failed)"
_MSG_COMPLETED_WITH_RETRY = "{result} (after {auto_retries} retry)"
_MSG_RESUMING = "Resuming from node '{from_node}'"
_MSG_SKIPPING = "Skipping nodes [{skip_nodes}], continuing downstream{suffix}"

# ---------------------------------------------------------------------------
# Notification rendering templates
# ---------------------------------------------------------------------------

_FMT_NOTIFICATION_STARTED = (
    '"{command}" Workflow started (run_id: {run_id}, definition_id: {definition_id})\n'
    "stage-summary:\n{stage_summary}"
)
_FMT_NOTIFICATION_SUCCESS = (
    '"{command}" Workflow succeeded (run_id: {run_id}, definition_id: {definition_id})\n' "result: {result}"
)
_FMT_NOTIFICATION_FAILED = (
    '"{command}" Workflow failed (run_id: {run_id}, definition_id: {definition_id})\n'
    "{error_block}"
    "DAG paused, all nodes finished.\n"
    "task params: {initial_params}\n"
    "failed nodes:\n{failed_nodes_text}\n"
    "waiting_for_route nodes:\n{waiting_nodes_text}\n"
    "completed nodes:\n{completed_nodes_text}\n"
    "skipped nodes:\n{skipped_nodes_text}\n"
    "pending nodes:\n{pending_nodes_text}"
)
_FMT_NODE_NOTIFICATION = (
    '"{command}" Workflow node_{event} (run_id: {run_id}, definition_id: {definition_id})\n'
    "{subject_label}:\n{subject_node_text}\n"
    "waiting_for_route nodes:\n{waiting_nodes_text}\n"
    "running nodes:\n{running_nodes_text}\n"
    "completed nodes:\n{completed_nodes_text}\n"
    "skipped nodes:\n{skipped_nodes_text}\n"
    "pending nodes:\n{pending_nodes_text}\n"
    "{action_hint}"
)
# Trailing action hint for node_failed — driven by whether any node is still
# running. No running node means the graph has stalled and a terminal failure
# follows immediately, so a decision is needed now. Both variants point at the
# diagnostic + recovery tools use the canonical run and definition identities.
_HINT_NODE_FAILURE_RECOVERY = (
    'Inspect with GetNodeStates(run_id="{run_id}", definition_id="{definition_id}") '
    'to see per-node status/inputs/outputs, then ResumeTasks(run_id="{run_id}", '
    'definition_id="{definition_id}", from_node=[...]) to re-run '
    "(or skip_node=[...] to bypass, overrides={{...}} to change inputs)."
)
_HINT_NODE_FAILURE_STALLED = (
    "No runnable nodes remain — make a decision now or ask the user. " + _HINT_NODE_FAILURE_RECOVERY
)
_HINT_NODE_FAILURE_RUNNING = "Other nodes are still running — you may decide later. " + _HINT_NODE_FAILURE_RECOVERY
_FMT_STALL_NOTIFICATION = (
    '"{command}" Workflow stalled (run_id: {run_id}, definition_id: {definition_id})\n'
    "The graph ran out of runnable nodes but did not finish: one or more AND-join "
    "nodes are deadlocked (a required upstream can never arrive). A decision is "
    "needed now.\n"
    "stalled join nodes:\n{stalled_nodes_text}\n"
    "completed nodes:\n{completed_nodes_text}\n"
    "skipped nodes:\n{skipped_nodes_text}\n"
    "pending nodes:\n{pending_nodes_text}\n"
    "{action_hint}"
)
# One stalled AND-join block: names the join, which of its sources arrived and
# which are still missing (the missing ones are what deadlocked it).
_FMT_STALL_NODE_BLOCK = (
    "  - {node_name}\n" "    arrived sources: {arrived_text}\n" "    missing sources: {missing_text}"
)
_HINT_STALL = (
    'Break the deadlock: ResumeTasks(run_id="{run_id}", definition_id="{definition_id}", '
    "from_node=[<missing upstream(s)>]) to run them, "
    "or skip_node=[<missing upstream(s)>] to bypass the join's wait (keeps partial results), "
    'or ask the user. Inspect first with GetNodeStates(run_id="{run_id}", '
    'definition_id="{definition_id}").'
)
_FMT_LLM_ROUTE_NOTIFICATION = (
    '"{command}" Workflow waiting_for_route (run_id: {run_id}, definition_id: {definition_id})\n'
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
    '  - ResumeTasks(run_id="{run_id}", definition_id="{definition_id}", '
    'from_node="{target_node}")  → {route_key}\n'
    "    description: {target_desc}\n"
    "    params:\n{target_params_text}"
)
# Route-pause hints. Each option above already shows its ``resume_tasks(...)``
# call; add a pointer to get_node_state for inspecting produced state before
# deciding.
_HINT_INSPECT = (
    'Call GetNodeStates(run_id="{run_id}", definition_id="{definition_id}") '
    "for per-node status, "
    "or fields=[...] to dump a produced value before choosing."
)
_HINT_OPTIONAL = "You may also do nothing (route to END requires no action). " + _HINT_INSPECT
_HINT_REQUIRED = "You MUST choose one of the above options to continue. " + _HINT_INSPECT

_FMT_FAILED_NODE_BLOCK = "  - {node_name}\n" "    error: {error}\n" "    auto_retries: {auto_retries_text}"
_FMT_NODE_PARAM = "      {name} = {value}\n        {desc}, {source}"
_FMT_WAITING_NODE_BLOCK = "  - {node_name}\n" "    description: {node_desc}\n" "    prompt: {prompt}"
# Bare-name block — the single template for every section that reports node
# identity only (the node subject on success, plus the completed / running /
# skipped / pending sections). No description, result, error or prompt.
_FMT_BARE_NODE_BLOCK = "  - {node_name}"
# Suffix appended to a ring node's success subject: it loops back to itself, so
# this is lap N (one per batch), not a retry — the graph will continue looping
# while work remains, then route onward.
_FMT_SELF_LOOP_LAP = " (ring: completed lap {lap}; loops back to itself per batch, will continue while work remains)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_auto_retries(run_state: Optional[GraphRunState], node_name: str) -> str:
    """Render the auto-retry tally for *node_name* from its run record.

    The counts live on the :class:`NodeRecord` (set by the engine on failure),
    not on the exception, so the run state is the single source of truth.
    """
    rec = run_state.get(node_name) if run_state is not None else None
    attempted = rec.retries_attempted if rec is not None else 0
    limit = rec.retries_limit if rec is not None else 0
    if limit == 0:
        return "0/0 (no auto-retry)"
    return f"{attempted}/{limit} (all failed)"


def _resolve_param_source(source: str, state: Any, initial_params: dict) -> Any:
    """Resolve a ``params['from']`` source against the state / initial params.

    A non-``$input`` source references a state *field* (first dotted segment),
    optionally indexed into by a ``.key`` for dict-valued fields.
    """
    if source.startswith("$input."):
        field_name = source[len("$input.") :]
        if field_name in initial_params:
            return initial_params.get(field_name)
        return getattr(state, field_name, None)
    parts = source.split(".", 1)
    field_name = parts[0]
    field_value = getattr(state, field_name, None)
    if len(parts) == 1:
        return field_value
    key = parts[1]
    if isinstance(field_value, dict):
        return field_value.get(key)
    return getattr(field_value, key, None)


def _render_node_params(node_name: str, graph: Any, state: Any) -> str:
    node_def = graph._nodes.get(node_name)
    if not node_def or not node_def.params:
        return "      (none)"
    lines = []
    for name, info in node_def.params.items():
        source = info["from"]
        source_text = (
            "task input param" if source.startswith("$input.") else f"from state field '{source.split('.')[0]}'"
        )
        value = _resolve_param_source(source, state, {})
        lines.append(
            _FMT_NODE_PARAM.format(name=name, value=repr(value), desc=info.get("desc", ""), source=source_text)
        )
    return "\n".join(lines) if lines else "      (none)"


def _waiting_names(graph: Any, completed: set) -> set:
    """Names of completed nodes parked on an LLM-route edge (waiting_for_route).

    These take precedence over the ``completed nodes`` section so a node is
    reported in exactly one place.
    """
    return {le.from_node for le in graph._llm_edges if le.from_node in completed and le.from_node in graph._nodes}


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
    graph: Any,
    state: Any,
    run_state: GraphRunState,
    completed: Optional[set] = None,
    failed_names: Optional[set] = None,
) -> str:
    """Render SUCCESS nodes (with a result). SKIPPED nodes go to their own
    section; nodes parked on an LLM route are reported under waiting_for_route,
    and nodes present in *failed_names* are reported under failed — both are
    excluded here so each node appears in exactly one section. The failed
    exclusion matters under cyclic graphs, where a node may fail and then
    succeed on a later loop (SUCCESS status while still in the failure list).

    Node status is read from the per-run :class:`GraphRunState`, not the shared
    graph definition, so concurrent runs of one compiled graph never cross-talk.
    """
    blocks = []
    names = completed if completed is not None else list(graph._nodes.keys())
    excluded = _waiting_names(graph, set(names)) | (failed_names or set())
    for name in names:
        if name not in graph._nodes or name in excluded:
            continue
        # Completion is authoritative on the run state — node results merge into
        # state *fields*, so there is no per-node value to gate on anymore.
        if run_state.get(name).status != WorkflowNodeStatus.SUCCESS:
            continue
        blocks.append(_FMT_BARE_NODE_BLOCK.format(node_name=name))
    return "\n".join(blocks) if blocks else "  (none)"


def _render_status_nodes(graph: Any, run_state: GraphRunState, status: WorkflowNodeStatus) -> str:
    """Render nodes whose run-state status equals *status* as bare name blocks.

    Used for the ``skipped`` and ``pending`` sections — these report node
    identity only, without result/error/prompt detail. Iterates the graph's
    declared nodes (stable order) and reads each status from the per-run record.
    """
    blocks = [
        _FMT_BARE_NODE_BLOCK.format(node_name=name) for name in graph._nodes if run_state.get(name).status == status
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


def _render_failed_nodes(
    error: Optional[BaseException], graph: Any, state: Any, run_state: Optional[GraphRunState]
) -> str:
    # Any error carrying a ``failures`` list (today: GraphBatchFailureError)
    # drives the per-node section — no hard isinstance coupling, so a future
    # batch-style error participates uniformly.
    failures = getattr(error, "failures", None) or []
    blocks = []
    for name, exc in _dedup_failures(failures):
        if name in graph._nodes:
            blocks.append(
                _FMT_FAILED_NODE_BLOCK.format(
                    node_name=name,
                    error=_as_text(exc),
                    auto_retries_text=_format_auto_retries(run_state, name),
                )
            )
    return "\n".join(blocks) if blocks else "  (none)"


def _render_terminal_error_block(error: Optional[BaseException]) -> str:
    """Render the top-level ``<error>`` envelope for a terminal graph failure.

    The per-node breakdown of a ``GraphBatchFailureError`` is already rendered —
    richer (dedup + auto-retry counts) — in the DAG ``failed nodes`` section, so
    the ``failures`` detail is stripped here to avoid a duplicate listing. The
    envelope still carries the machine-readable ``code`` / ``recovery`` /
    ``retryable`` and the cause, which is the *only* place those surface for
    non-batch fatal errors (router / recursion) that populate no failed node.
    """
    if error is None:
        return ""
    report = ErrorReport.from_exception(error)
    if "failures" in report.detail:
        report = replace(report, detail={k: v for k, v in report.detail.items() if k != "failures"})
    return render_error_block(report)


def _render_running_nodes(running_names: list[str], graph: Any) -> str:
    blocks = [_FMT_BARE_NODE_BLOCK.format(node_name=name) for name in running_names if name in graph._nodes]
    return "\n".join(blocks) if blocks else "  (none)"


def _workflow_identity(graph: Any, run_state: GraphRunState) -> tuple[WorkflowRunId, WorkflowDefinitionId]:
    definition_id = graph._definition_id
    if not isinstance(definition_id, WorkflowDefinitionId):
        raise RuntimeError("Workflow notification requires a canonical definition identity")
    return WorkflowRunId(run_state.activity_execution_id), definition_id


# ---------------------------------------------------------------------------
# Structured notification model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowNotification:
    """Structured Workflow notification for LLM consumption."""

    run_id: WorkflowRunId
    definition_id: WorkflowDefinitionId
    command: str
    status: WorkflowNodeStatus
    event: str  # "started" | "node_failed" | "terminal"
    result: Optional[str] = None
    initial_params: Optional[dict] = None
    failed_nodes_text: str = "  (none)"
    completed_nodes_text: str = "  (none)"
    waiting_nodes_text: str = "  (none)"
    skipped_nodes_text: str = "  (none)"
    pending_nodes_text: str = "  (none)"
    # Rendered <error> block for the top-level terminal failure (router /
    # recursion / batch). Carries the typed code+recovery so the cause is
    # visible even when no node populated the failed-nodes section.
    error_block: str = ""

    def to_text(self) -> str:
        if self.status == WorkflowNodeStatus.FAILED:
            return _FMT_NOTIFICATION_FAILED.format(
                command=self.command,
                run_id=self.run_id,
                definition_id=self.definition_id,
                error_block=f"{self.error_block}\n" if self.error_block else "",
                initial_params=self.initial_params or {},
                failed_nodes_text=self.failed_nodes_text,
                completed_nodes_text=self.completed_nodes_text,
                waiting_nodes_text=self.waiting_nodes_text,
                skipped_nodes_text=self.skipped_nodes_text,
                pending_nodes_text=self.pending_nodes_text,
            )
        return _FMT_NOTIFICATION_SUCCESS.format(
            command=self.command,
            run_id=self.run_id,
            definition_id=self.definition_id,
            result=_as_text(self.result) if self.result else "",
        )


# ---------------------------------------------------------------------------
# Push functions (all route through report_progress)
# ---------------------------------------------------------------------------


def push_started_notification(
    graph: Any,
    run_state: GraphRunState,
) -> None:
    run_id, definition_id = _workflow_identity(graph, run_state)
    text = _FMT_NOTIFICATION_STARTED.format(
        command=graph.command_name,
        run_id=run_id,
        definition_id=definition_id,
        stage_summary=graph._build_stage_summary(),
    )
    emit_workflow_progress(graph, run_state, END, WorkflowNodeStatus.RUNNING, text)


def push_terminal_notification(
    graph: Any,
    state: Any,
    status: WorkflowNodeStatus,
    *,
    result: Any = None,
    error: Optional[BaseException] = None,
    initial_params: Optional[dict] = None,
    run_state: Optional[GraphRunState] = None,
) -> None:
    run_state = GraphRunState.ensure(graph, state, run_state)
    run_id, definition_id = _workflow_identity(graph, run_state)
    failures = getattr(error, "failures", None) or []
    failed_names = {name for name, _ in failures}
    error_block = _render_terminal_error_block(error)
    tn = WorkflowNotification(
        run_id=run_id,
        definition_id=definition_id,
        command=graph.command_name,
        status=status,
        event="terminal",
        result=_as_text(result) if result else None,
        initial_params=initial_params,
        error_block=error_block,
        failed_nodes_text=_render_failed_nodes(error, graph, state, run_state),
        # Exclude failed nodes so a cyclic fail→succeed node is not double-listed.
        completed_nodes_text=_render_completed_nodes(graph, state, run_state, failed_names=failed_names),
        # The graph is never paused on an LLM route at terminal → no waiting node.
        waiting_nodes_text=_render_waiting_nodes(graph, set()),
        skipped_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.SKIPPED),
        pending_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.PENDING),
    )
    emit_workflow_progress(graph, run_state, END, status, tn.to_text())


def push_node_notification(
    node_name: str,
    status: WorkflowNodeStatus,
    state: Any,
    graph: Any,
    *,
    completed: set,
    running_names: list[str],
    run_state: Optional[GraphRunState] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Push a structured notification for any node terminal status change.

    Works for success, failure, cancellation — same graph-snapshot format.
    """
    run_state = GraphRunState.ensure(graph, state, run_state)
    run_id, definition_id = _workflow_identity(graph, run_state)
    if status == WorkflowNodeStatus.FAILED and exc is not None:
        subject_label = "node fail"
        event = "failed"
        subject_text = _FMT_FAILED_NODE_BLOCK.format(
            node_name=node_name,
            error=_as_text(exc),
            auto_retries_text=_format_auto_retries(run_state, node_name),
        )
    else:
        subject_label = "node success"
        event = "completed"
        subject_text = _FMT_BARE_NODE_BLOCK.format(node_name=node_name)
        # A ring node completes once per lap, so it emits many identical success
        # notifications. Annotate the lap so the repetition reads as progress
        # (walking a work list one batch at a time), not a stall or restart.
        if graph.is_self_loop(node_name):
            lap = run_state.get(node_name).attempts
            subject_text += _FMT_SELF_LOOP_LAP.format(lap=lap)

    action_hint = ""
    if status == WorkflowNodeStatus.FAILED:
        action_hint = (_HINT_NODE_FAILURE_RUNNING if running_names else _HINT_NODE_FAILURE_STALLED).format(
            run_id=run_id, definition_id=definition_id
        )

    notification = _FMT_NODE_NOTIFICATION.format(
        command=graph.command_name,
        run_id=run_id,
        definition_id=definition_id,
        event=event,
        subject_label=subject_label,
        subject_node_text=subject_text,
        completed_nodes_text=_render_completed_nodes(graph, state, run_state, completed),
        running_nodes_text=_render_running_nodes(running_names, graph),
        waiting_nodes_text=_render_waiting_nodes(graph, completed),
        skipped_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.SKIPPED),
        pending_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.PENDING),
        action_hint=action_hint,
    )
    emit_workflow_progress(graph, run_state, node_name, status, notification)


def push_llm_route_notification(
    llm_edge: Any,
    state: Any,
    graph: Any,
    run_state: GraphRunState,
) -> None:
    run_id, definition_id = _workflow_identity(graph, run_state)
    from_node_def = graph._nodes.get(llm_edge.from_node)
    from_node_desc = from_node_def.description if from_node_def else ""
    # Field/channel model: there is no per-node result slot, so surface a
    # compact snapshot of the full state instead of one node's value.
    from_node_result = _as_text(state.model_dump())

    has_end = END in llm_edge.mapping.values()
    options = []
    for route_key, target_node in llm_edge.mapping.items():
        if target_node == END:
            continue
        target_def = graph._nodes.get(target_node)
        target_desc = target_def.description if target_def else ""
        options.append(
            _FMT_LLM_ROUTE_OPTION.format(
                run_id=run_id,
                definition_id=definition_id,
                target_node=target_node,
                route_key=route_key,
                target_desc=target_desc,
                target_params_text=_render_node_params(target_node, graph, state),
            )
        )
    options_text = "\n".join(options) if options else "  (none)"
    action_hint = (_HINT_OPTIONAL if has_end else _HINT_REQUIRED).format(run_id=run_id, definition_id=definition_id)

    notification = _FMT_LLM_ROUTE_NOTIFICATION.format(
        command=graph.command_name,
        run_id=run_id,
        definition_id=definition_id,
        stage_summary=graph._build_stage_summary(),
        from_node=llm_edge.from_node,
        from_node_desc=from_node_desc,
        from_node_result=from_node_result,
        prompt=llm_edge.prompt,
        options_text=options_text,
        action_hint=action_hint,
    )
    emit_workflow_progress(
        graph,
        run_state,
        llm_edge.from_node,
        WorkflowNodeStatus.WAITING_FOR_ROUTE,
        notification,
    )


def _render_stall_nodes(graph: Any, stalled_nodes: tuple[str, ...], completed: set) -> str:
    """Render each deadlocked AND-join with its arrived / missing sources.

    A join's ``sources`` come from the graph's waiting-edges; a source counts as
    arrived when it is in *completed*. The missing sources are what deadlocked
    the join, so they are the model's resume/skip targets.
    """
    blocks = []
    for name in stalled_nodes:
        # A target may have several waiting-edges; union their sources.
        sources: list[str] = []
        for we in graph._waiting_edges:
            if we.to_node == name:
                for s in we.sources:
                    if s not in sources:
                        sources.append(s)
        arrived = [s for s in sources if s in completed]
        missing = [s for s in sources if s not in completed]
        blocks.append(
            _FMT_STALL_NODE_BLOCK.format(
                node_name=name,
                arrived_text=", ".join(arrived) if arrived else "(none)",
                missing_text=", ".join(missing) if missing else "(none)",
            )
        )
    return "\n".join(blocks) if blocks else "  (none)"


def push_stall_notification(
    graph: Any,
    state: Any,
    stalled_nodes: tuple[str, ...],
    *,
    completed: set,
    run_state: Optional[GraphRunState] = None,
) -> None:
    """Push a decision notification when the frontier drains on a deadlocked join.

    Mirrors the LLM-route pause: the task is not terminal, so the pool owns the
    push (this renders the rich DAG snapshot to disk as the ``<task-attachment>``
    source of truth). Names the stalled joins with their missing upstreams so the
    model can resume/skip them directly.
    """
    run_state = GraphRunState.ensure(graph, state, run_state)
    run_id, definition_id = _workflow_identity(graph, run_state)
    notification = _FMT_STALL_NOTIFICATION.format(
        command=graph.command_name,
        run_id=run_id,
        definition_id=definition_id,
        stalled_nodes_text=_render_stall_nodes(graph, stalled_nodes, completed),
        completed_nodes_text=_render_completed_nodes(graph, state, run_state, completed),
        skipped_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.SKIPPED),
        pending_nodes_text=_render_status_nodes(graph, run_state, WorkflowNodeStatus.PENDING),
        action_hint=_HINT_STALL.format(run_id=run_id, definition_id=definition_id),
    )
    emit_workflow_progress(
        graph,
        run_state,
        END,
        WorkflowNodeStatus.STALLED,
        notification,
    )
