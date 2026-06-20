"""Frontier scheduler for :mod:`metagpt.executor.bggraph`.

Execution model (langgraph transitions, **not** static topological DAG):

* A frontier of nodes is spawned as ``asyncio`` tasks and reaped as-completed.
* When a node finishes, :func:`successors` computes its next hops *forward*:
  a single edge fires immediately, a waiting-edge is an AND-join (fires only
  once all sources have arrived), a conditional edge runs its router on the
  post-completion state, and an LLM edge pauses the whole graph.
* Cycles are allowed; ``recursion_limit`` bounds total node activations.
* Parallel nodes fail independently: a node failure pushes an immediate
  notification and the remaining frontier keeps running.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Optional

from metagpt.common.exception import RecoveryAction, RecoveryRunner
from metagpt.common.exception.graph import GraphNodeRetryExhaustedError, GraphNodeTimeoutError
from metagpt.executor.tasks.types import BgTaskResult, GraphMeta
from metagpt.executor.tasks.bggraph.channels import apply_updates
from metagpt.executor.tasks.bggraph.notify import (
    _MSG_RESUMING,
    _MSG_SKIPPING,
    push_llm_route_notification,
    push_node_notification,
    push_started_notification,
    push_terminal_notification,
)
from metagpt.executor.tasks.bggraph.notify import (
    _MSG_RETRYING,
)
from metagpt.executor.tasks.bggraph.report import report_progress
from metagpt.executor.tasks.bggraph.types import (
    END,
    GraphBatchFailureError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
    GraphRunState,
    GraphState,
    LlmPauseResult,
    BgStatus,
    Stage,
)

if TYPE_CHECKING:
    from metagpt.executor.tasks.bggraph.graph import BgGraph


class _LlmPauseSignal(Exception):
    """Internal: raised by :func:`successors` to pause on an LLM edge."""

    def __init__(self, edge: Any):
        self.edge = edge
        super().__init__(f"LLM route pause at '{edge.from_node}'")


# Framework retry policy — applied uniformly to every node, not user-tunable.
# A node has no retry knobs of its own; the engine alone owns the "how many /
# how long" so that retry semantics stay consistent across the whole graph.
_AUTO_RETRIES = 3  # max retries per node for transient (RETRY-classified) failures
_RETRY_WAIT = 1.0  # base backoff seconds (grows exponentially per attempt)
# Cap for exponential backoff — a single retry never sleeps longer than this,
# mirroring the LLM layer's ``wait_random_exponential(max=...)`` ceiling.
_MAX_BACKOFF_WAIT = 60.0


def _retry_delay(attempt: int) -> float:
    """Compute the sleep before retry *attempt* (1-based).

    Exponential backoff with full jitter: the wait grows as
    ``_RETRY_WAIT * 2**(attempt-1)``, is capped at :data:`_MAX_BACKOFF_WAIT`,
    then has full jitter applied (uniform in ``[0, capped]``) to de-correlate
    concurrent nodes — the same shape as tenacity's ``wait_random_exponential``.
    """
    ceiling = min(_RETRY_WAIT * (2 ** (attempt - 1)), _MAX_BACKOFF_WAIT)
    return random.uniform(0, ceiling)


# ---------------------------------------------------------------------------
# Node execution
# ---------------------------------------------------------------------------

_MISSING = object()


def _validate_node_params_runtime(graph: "BgGraph", node_name: str, state: GraphState) -> None:
    """Check that state values match declared param types before node execution.

    Uses pydantic ``TypeAdapter(strict=True)`` for deep validation (generics,
    Union, nested models). Raises :class:`GraphParamTypeError` on mismatch —
    a wiring bug, not transient.
    """
    from pydantic import TypeAdapter, ValidationError

    node_def = graph._nodes[node_name]
    for param_name, param_info in node_def.params.items():
        expected_type = param_info.get("type")
        if expected_type is None:
            continue
        source = param_info["from"]
        if not source:
            continue
        # Resolve the attribute name on state
        if source.startswith("$input."):
            field = source[len("$input."):]
        else:
            field = source.split(".")[0]
        value = getattr(state, field, _MISSING)
        if value is _MISSING or value is None:
            continue  # missing/None — ref check already caught at compile time
        try:
            TypeAdapter(expected_type).validate_python(value, strict=True)
        except ValidationError:
            raise GraphParamTypeError(
                node=node_name, param=param_name,
                expected=expected_type, got=type(value),
            )


async def _run_poll(stage: Stage, submit_result: Any) -> Any:
    poll_coro = stage.poll(submit_result)
    if stage.timeout:
        return await asyncio.wait_for(poll_coro, stage.timeout)
    return await poll_coro


async def _run_one_node(
    node_name: str,
    state: GraphState,
    graph: "BgGraph",
    completed: set,
    run_state: Optional[GraphRunState] = None,
) -> None:
    """Execute one node: fn -> submit -> poll (if any) -> auto-retry -> store result.

    Retries run under the shared :class:`RecoveryRunner` using the framework's
    fixed policy (:data:`_AUTO_RETRIES` attempts, exponential backoff): the node
    executes as the runner's ``call``, and a RETRY strategy backs off and re-runs
    it. The runner classifies each failure (typed ``recovery`` hint else
    :func:`is_retryable`), so only transient failures (network/timeout/typed
    RetryableError) consume the retry budget; a permanent error (e.g.
    ``ValueError``) is classified ABORT and fails fast. The retry budget is not
    node-configurable — the engine owns it so semantics stay uniform — but the
    number of retries consumed is recorded on the run record for reporting.

    ``run_state`` (when provided) receives the authoritative per-node record
    (status / attempts / failure reason / retries) — the single source the
    notification renderer and resume both read. The shared graph definition is
    never mutated, so a compiled graph stays safe to reuse across concurrent runs.
    """
    node_def = graph._nodes[node_name]
    if run_state is not None:
        run_state.mark_running(node_name)
    report_progress(node_name, BgStatus.RUNNING, node_def.description or None)

    attempts = 0

    async def _execute() -> Any:
        nonlocal attempts
        attempts += 1
        _validate_node_params_runtime(graph, node_name, state)
        try:
            stage = await node_def.fn(state)
            submit_result = await stage.submit
            if stage.poll is not None:
                return await _run_poll(stage, submit_result)
            return submit_result
        except (asyncio.TimeoutError, TimeoutError, ConnectionError) as e:
            raise GraphNodeTimeoutError(str(e), cause=e) from e

    async def _retry(exc: BaseException) -> bool:
        # Reached only when the runner classifies ``exc`` as RETRY. ``attempts``
        # already counts the failed attempt, so it is the 1-based retry number.
        report_progress(
            node_name,
            BgStatus.RUNNING,
            _MSG_RETRYING.format(
                attempt=attempts,
                auto_retries=_AUTO_RETRIES,
                error_type=type(exc).__name__,
                error=exc,
            ),
        )
        await asyncio.sleep(_retry_delay(attempts))
        return True

    runner = RecoveryRunner({RecoveryAction.RETRY: _retry}, max_recoveries=_AUTO_RETRIES)

    try:
        result = await runner.run(_execute)
    except asyncio.CancelledError:
        if run_state is not None:
            run_state.mark_cancelled(node_name)
        report_progress(node_name, BgStatus.CANCELLED)
        raise
    except GraphNodeTimeoutError as e:
        # Retry budget exhausted on a timeout — wrap as terminal.
        attempt = attempts - 1
        exhausted = GraphNodeRetryExhaustedError(node_name, attempts, e.__cause__ or e)
        if run_state is not None:
            run_state.mark_failed(
                node_name, exhausted, retries_attempted=attempt, retries_limit=_AUTO_RETRIES
            )
        raise exhausted from e
    except Exception as e:  # noqa: BLE001 — node failures are reported, not swallowed
        attempt = attempts - 1  # retries consumed before giving up
        if run_state is not None:
            run_state.mark_failed(
                node_name, e, retries_attempted=attempt, retries_limit=_AUTO_RETRIES
            )
        raise

    # Field/channel state sync: a node returns a dict of field updates which is
    # merged into the declared state fields (reducer channels combine, plain
    # fields are last-value). ``None`` means "no update". A non-dict return is a
    # wiring bug (un-migrated node) and fails loudly.
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise GraphParamTypeError(
            node=node_name, param="<return>", expected=dict, got=type(result)
        )
    apply_updates(state, result, graph._reducers)
    completed.add(node_name)
    if run_state is not None:
        run_state.mark_success(node_name)


# ---------------------------------------------------------------------------
# Forward transition
# ---------------------------------------------------------------------------


def successors(
    node: str,
    graph: "BgGraph",
    state: GraphState,
    completed: set,
    trigger_count: dict,
    run_state: Optional[GraphRunState] = None,
) -> list[str]:
    """Compute the next hops after *node* completes.

    Raises :class:`_LlmPauseSignal` for an LLM edge and :class:`GraphRouterError`
    for a failing / unknown conditional router.

    When *run_state* is provided, the conditional router's chosen key is recorded
    on the node's record (``last_route_key``) for observability — the route taken
    is otherwise lost once the target node is appended to the frontier.
    """
    # 1. LLM edge takes precedence → pause the whole graph.
    for le in graph._llm_edges:
        if le.from_node == node:
            raise _LlmPauseSignal(le)

    out: list[str] = []

    # 2. Conditional edges — run router on the post-completion state.
    for ce in graph._conditional_edges:
        if ce.from_node != node:
            continue
        try:
            key = ce.router(state)
        except Exception as e:  # noqa: BLE001
            raise GraphRouterError(f"Router on '{node}' raised {type(e).__name__}: {e}") from e
        if key not in ce.mapping:
            raise GraphRouterError(
                f"Router on '{node}' returned '{key}', not in mapping {list(ce.mapping.keys())}"
            )
        if run_state is not None:
            run_state.get(node).last_route_key = key
        out.append(ce.mapping[key])

    # 3. Single static edges — fire immediately.
    for e in graph._edges:
        if e.from_node == node:
            out.append(e.to_node)

    # 4. Waiting-edges — AND-join, fire only once all sources have arrived.
    #    The arrival set is per-activation: once every source has arrived and
    #    the join fires, reset it so a later cycle must re-collect all sources
    #    afresh (mirrors LangGraph's NamedBarrierValue.consume). Without the
    #    reset the set stays full and an AND-join inside a cycle silently
    #    degrades to an OR-join on the second and later laps.
    for we in graph._waiting_edges:
        if node in we.sources:
            trigger_count[we.to_node].add(node)
            if set(we.sources).issubset(trigger_count[we.to_node]):
                out.append(we.to_node)
                trigger_count[we.to_node].clear()

    return out


def _collect_finish_result(graph: "BgGraph", state: GraphState) -> Any:
    # Field/channel model: the run result is the full final state (langgraph
    # ``.invoke()`` semantics), not a per-finish-node slot value.
    return state.model_dump()


async def _cancel_running(running: dict) -> None:
    for t in list(running):
        t.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    running.clear()


def _prefill_trigger_count(graph: "BgGraph", completed: set, trigger_count: dict) -> None:
    """Seed waiting-edge arrivals from already-completed sources (resume)."""
    for we in graph._waiting_edges:
        for s in we.sources:
            if s in completed:
                trigger_count[we.to_node].add(s)


# ---------------------------------------------------------------------------
# Core driver
# ---------------------------------------------------------------------------


async def _run_driver(
    graph: "BgGraph",
    state: GraphState,
    *,
    execute_nodes: list[str],
    precompleted: tuple[str, ...] = (),
    completed: Optional[set] = None,
    trigger_count: Optional[dict] = None,
    initial_params: Optional[dict] = None,
    push_start: bool = True,
    run_state: Optional[GraphRunState] = None,
) -> Any:
    """Run the frontier scheduler to terminal / pause.

    Args:
        execute_nodes: nodes to spawn (execute) initially.
        precompleted: nodes treated as just-finished — their successors are
            seeded into the frontier without re-executing them (resume_skip).
        completed: pre-seeded completed set (resume).
        trigger_count: pre-seeded AND-join arrivals (resume).
        run_state: authoritative per-node records; created for this graph if
            ``None``. Carried out on the pause result / failure exception so the
            pool can snapshot it onto ``TaskMeta`` for resume.
    """
    completed = completed if completed is not None else set()
    trigger_count = trigger_count if trigger_count is not None else defaultdict(set)
    if run_state is None:
        run_state = GraphRunState.for_graph(graph)

    running: dict[asyncio.Task, str] = {}
    scheduled: set[str] = set()
    all_errors: list[tuple[str, BaseException]] = []
    activations = 0

    if push_start:
        push_started_notification(graph)

    def spawn(node: str) -> None:
        nonlocal activations
        # Dedup only on the in-flight set (``scheduled``): a node already
        # running/queued is not spawned twice in the same wave. Completed nodes
        # CAN be re-spawned — that is how cycles re-activate (bounded by
        # ``recursion_limit``). Use waiting-edges for AND/once fan-in semantics.
        if node == END or node in scheduled:
            return
        activations += 1
        if activations > graph.recursion_limit:
            raise GraphRecursionError(
                f"recursion_limit ({graph.recursion_limit}) exceeded "
                f"after {activations} node activations"
            )
        scheduled.add(node)
        t = asyncio.create_task(_run_one_node(node, state, graph, completed, run_state))
        running[t] = node

    pause_edge = None
    fatal: Optional[BaseException] = None

    try:
        for n in precompleted:
            for s in successors(n, graph, state, completed, trigger_count, run_state):
                spawn(s)
        for n in execute_nodes:
            spawn(n)

        while running:
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                n = running.pop(t)
                scheduled.discard(n)
                exc = t.exception()
                if exc is not None:
                    all_errors.append((n, exc))
                    push_node_notification(
                        n,
                        BgStatus.FAILED,
                        state,
                        graph,
                        completed=completed,
                        running_names=list(running.values()),
                        run_state=run_state,
                        exc=exc,
                    )
                    continue
                push_node_notification(
                    n,
                    BgStatus.SUCCESS,
                    state,
                    graph,
                    completed=completed,
                    running_names=list(running.values()),
                    run_state=run_state,
                )
                try:
                    succs = successors(n, graph, state, completed, trigger_count, run_state)
                except _LlmPauseSignal as sig:
                    pause_edge = sig.edge
                    raise
                for s in succs:
                    spawn(s)
    except _LlmPauseSignal:
        await _cancel_running(running)
        push_llm_route_notification(pause_edge, state, graph)
        return LlmPauseResult(
            state=state, completed=completed, edge=pause_edge, run_state=run_state
        )
    except (GraphRecursionError, GraphRouterError) as e:
        await _cancel_running(running)
        fatal = e

    if fatal is not None:
        # Carry the snapshot out on the exception so the pool captures it for
        # resume even when the GraphMeta handoff did not thread run_state in.
        fatal.run_state = run_state
        fatal.graph_state = state
        push_terminal_notification(
            graph, state, BgStatus.FAILED, error=fatal, initial_params=initial_params,
            run_state=run_state,
        )
        raise fatal

    if all_errors:
        error = GraphBatchFailureError(all_errors)
        error.run_state = run_state
        error.graph_state = state
        push_terminal_notification(
            graph, state, BgStatus.FAILED, error=error, initial_params=initial_params,
            run_state=run_state,
        )
        raise error

    result = _collect_finish_result(graph, state)
    push_terminal_notification(
        graph, state, BgStatus.SUCCESS, result=result, initial_params=initial_params,
        run_state=run_state,
    )
    return result


# ---------------------------------------------------------------------------
# compile → executor
# ---------------------------------------------------------------------------


def _build_executor(graph: "BgGraph"):
    """Return an async ``executor(**initial_state) -> BgTaskResult``."""

    async def executor(**initial_state) -> BgTaskResult:
        state = graph.state_schema(**initial_state)
        entry_nodes = graph._get_entry_nodes()
        initial_params = dict(initial_state)
        # One run_state, shared between the driver coroutine and the GraphMeta
        # handoff so the pool snapshots the very object the driver mutates.
        run_state = GraphRunState.for_graph(graph)
        return BgTaskResult.background(
            poll_factory=lambda: _run_driver(
                graph,
                state,
                execute_nodes=entry_nodes,
                completed=set(),
                trigger_count=defaultdict(set),
                initial_params=initial_params,
                run_state=run_state,
            ),
            command_name=graph.command_name,
            graph_meta=GraphMeta(
                graph_ref=graph,
                initial_params=initial_params,
                factory=executor,
                run_state=run_state,
                state=state,
            ),
        )

    return executor


# ---------------------------------------------------------------------------
# Resume variants
# ---------------------------------------------------------------------------


def _ensure_run_state(
    graph: "BgGraph", state: GraphState, run_state: Optional[GraphRunState]
) -> GraphRunState:
    """Return an authoritative run state, inferring one for legacy snapshots.

    A live task always carries its run_state on the snapshot; only tasks whose
    snapshot predates run-state recording fall back to value-inference.
    """
    if run_state is not None:
        return run_state
    return GraphRunState.infer_from_state(graph, state)


def resume(
    graph: "BgGraph",
    state: GraphState,
    from_nodes: list[str],
    run_state: Optional[GraphRunState] = None,
) -> BgTaskResult:
    """Resume execution by re-running *from_nodes*.

    Completed nodes come from the authoritative ``run_state`` (status SUCCESS /
    SKIPPED), not from value-inference. The graph then transitions forward from
    *from_nodes*; AND-joins are prefilled so a merge that already had one source
    satisfied does not stall. Re-run nodes are ``reset`` (PENDING) while keeping
    their accumulated attempt count.
    """
    run_state = _ensure_run_state(graph, state, run_state)
    completed: set[str] = run_state.completed_names() - set(from_nodes)
    for node_name in from_nodes:
        run_state.reset(node_name)

    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    return BgTaskResult.hybrid(
        result=_MSG_RESUMING.format(from_node=", ".join(from_nodes)),
        poll_factory=lambda: _run_driver(
            graph,
            state,
            execute_nodes=list(from_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
            run_state=run_state,
        ),
        command_name=graph.command_name,
        graph_meta=GraphMeta(graph_ref=graph, run_state=run_state, state=state),
    )


def _apply_skip(
    graph: "BgGraph",
    state: GraphState,
    skip_nodes: list[str],
    run_state: Optional[GraphRunState] = None,
) -> None:
    for sn in skip_nodes:
        if run_state is not None:
            run_state.mark_skipped(sn)
        report_progress(
            sn, BgStatus.SKIPPED, _MSG_SKIPPING.format(skip_nodes=sn, suffix="")
        )


def resume_skip(
    graph: "BgGraph",
    state: GraphState,
    skip_nodes: list[str],
    run_state: Optional[GraphRunState] = None,
) -> BgTaskResult:
    """Skip *skip_nodes* (keeping partial results) and continue downstream."""
    run_state = _ensure_run_state(graph, state, run_state)
    _apply_skip(graph, state, skip_nodes, run_state)
    completed: set[str] = run_state.completed_names() | set(skip_nodes)
    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    return BgTaskResult.hybrid(
        result=_MSG_SKIPPING.format(skip_nodes=", ".join(skip_nodes), suffix=""),
        poll_factory=lambda: _run_driver(
            graph,
            state,
            execute_nodes=[],
            precompleted=tuple(skip_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
            run_state=run_state,
        ),
        command_name=graph.command_name,
        graph_meta=GraphMeta(graph_ref=graph, run_state=run_state, state=state),
    )


def resume_skip_and_from(
    graph: "BgGraph",
    state: GraphState,
    skip_nodes: list[str],
    from_nodes: list[str],
    run_state: Optional[GraphRunState] = None,
) -> BgTaskResult:
    """Skip *skip_nodes* then re-run *from_nodes*, continuing downstream."""
    run_state = _ensure_run_state(graph, state, run_state)
    _apply_skip(graph, state, skip_nodes, run_state)
    completed: set[str] = (run_state.completed_names() | set(skip_nodes)) - set(from_nodes)
    for node_name in from_nodes:
        run_state.reset(node_name)

    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    skip_desc = ", ".join(skip_nodes)
    from_desc = ", ".join(from_nodes)
    return BgTaskResult.hybrid(
        result=f"Skipping [{skip_desc}], resuming from [{from_desc}]",
        poll_factory=lambda: _run_driver(
            graph,
            state,
            execute_nodes=list(from_nodes),
            precompleted=tuple(skip_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
            run_state=run_state,
        ),
        command_name=graph.command_name,
        graph_meta=GraphMeta(graph_ref=graph, run_state=run_state, state=state),
    )
