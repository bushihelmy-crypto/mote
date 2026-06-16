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
from typing import Any, Optional

from metagpt.common.exception import RecoveryAction, RecoveryRunner
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.tasks.bggraph.notify import (
    _MSG_RESUMING,
    _MSG_SKIPPING,
    push_llm_route_notification,
    push_node_failure_notification,
    push_started_notification,
    push_terminal_notification,
)
from metagpt.executor.tasks.bggraph.notify import (
    _MSG_COMPLETED_WITH_RETRY,
    _MSG_FAILED,
    _MSG_FAILED_WITH_RETRY,
    _MSG_RETRYING,
)
from metagpt.executor.tasks.bggraph.report import report_progress
from metagpt.executor.tasks.bggraph.types import (
    END,
    GraphBatchFailureError,
    GraphRecursionError,
    GraphRouterError,
    LlmPauseResult,
    BgStatus,
    Stage,
)


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


async def _run_poll(stage: Stage, submit_result: Any) -> Any:
    poll_coro = stage.poll(submit_result)
    if stage.timeout:
        return await asyncio.wait_for(poll_coro, stage.timeout)
    return await poll_coro


async def _run_one_node(node_name: str, state: Any, graph: Any, completed: set) -> None:
    """Execute one node: fn -> submit -> poll (if any) -> auto-retry -> store result.

    Retries run under the shared :class:`RecoveryRunner` using the framework's
    fixed policy (:data:`_AUTO_RETRIES` attempts, exponential backoff): the node
    executes as the runner's ``call``, and a RETRY strategy backs off and re-runs
    it. The runner classifies each failure (typed ``recovery`` hint else
    :func:`is_retryable`), so only transient failures (network/timeout/typed
    RetryableError) consume the retry budget; a permanent error (e.g.
    ``ValueError``) is classified ABORT and fails fast. The retry budget is not
    node-configurable — the engine owns it so semantics stay uniform — but the
    number of retries consumed is recorded on the exception for reporting.
    """
    node_def = graph._nodes[node_name]
    node_def.status = BgStatus.RUNNING
    report_progress(node_name, BgStatus.RUNNING, node_def.description or None)

    attempts = 0

    async def _execute() -> Any:
        nonlocal attempts
        attempts += 1
        stage = await node_def.fn(state)
        submit_result = await stage.submit
        if stage.poll is not None:
            return await _run_poll(stage, submit_result)
        return submit_result

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
        node_def.status = BgStatus.CANCELLED
        report_progress(node_name, BgStatus.CANCELLED)
        raise
    except Exception as e:  # noqa: BLE001 — node failures are reported, not swallowed
        attempt = attempts - 1  # retries consumed before giving up
        detail = (
            _MSG_FAILED_WITH_RETRY.format(
                error_type=type(e).__name__,
                error=e,
                attempt=attempt,
                auto_retries=_AUTO_RETRIES,
            )
            if attempt > 0
            else _MSG_FAILED.format(error_type=type(e).__name__, error=e)
        )
        report_progress(node_name, BgStatus.FAILED, detail)
        node_def.status = BgStatus.FAILED
        e._auto_retries_attempted = attempt
        e._auto_retries_limit = _AUTO_RETRIES
        raise

    setattr(state, node_name, result)
    completed.add(node_name)
    node_def.status = BgStatus.SUCCESS
    attempt = attempts - 1  # retries consumed before succeeding
    if attempt > 0:
        detail = _MSG_COMPLETED_WITH_RETRY.format(result=result, auto_retries=attempt)
    else:
        detail = result
    report_progress(node_name, BgStatus.SUCCESS, detail)


# ---------------------------------------------------------------------------
# Forward transition
# ---------------------------------------------------------------------------


def successors(node: str, graph: Any, state: Any, completed: set, trigger_count: dict) -> list[str]:
    """Compute the next hops after *node* completes.

    Raises :class:`_LlmPauseSignal` for an LLM edge and :class:`GraphRouterError`
    for a failing / unknown conditional router.
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
        out.append(ce.mapping[key])

    # 3. Single static edges — fire immediately.
    for e in graph._edges:
        if e.from_node == node:
            out.append(e.to_node)

    # 4. Waiting-edges — AND-join, fire only once all sources have arrived.
    for we in graph._waiting_edges:
        if node in we.sources:
            trigger_count[we.to_node].add(node)
            if set(we.sources).issubset(trigger_count[we.to_node]):
                out.append(we.to_node)

    return out


def _collect_finish_result(graph: Any, state: Any) -> Any:
    finish_nodes = graph._get_finish_nodes()
    if len(finish_nodes) == 1:
        return getattr(state, finish_nodes[0])
    return {n: getattr(state, n) for n in finish_nodes if getattr(state, n, None) is not None}


async def _cancel_running(running: dict) -> None:
    for t in list(running):
        t.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    running.clear()


def _prefill_trigger_count(graph: Any, completed: set, trigger_count: dict) -> None:
    """Seed waiting-edge arrivals from already-completed sources (resume)."""
    for we in graph._waiting_edges:
        for s in we.sources:
            if s in completed:
                trigger_count[we.to_node].add(s)


# ---------------------------------------------------------------------------
# Core driver
# ---------------------------------------------------------------------------


async def _run_driver(
    graph: Any,
    state: Any,
    *,
    execute_nodes: list[str],
    precompleted: tuple[str, ...] = (),
    completed: Optional[set] = None,
    trigger_count: Optional[dict] = None,
    initial_params: Optional[dict] = None,
    push_start: bool = True,
) -> Any:
    """Run the frontier scheduler to terminal / pause.

    Args:
        execute_nodes: nodes to spawn (execute) initially.
        precompleted: nodes treated as just-finished — their successors are
            seeded into the frontier without re-executing them (resume_skip).
        completed: pre-seeded completed set (resume).
        trigger_count: pre-seeded AND-join arrivals (resume).
    """
    completed = completed if completed is not None else set()
    trigger_count = trigger_count if trigger_count is not None else defaultdict(set)

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
        t = asyncio.create_task(_run_one_node(node, state, graph, completed))
        running[t] = node

    pause_edge = None
    fatal: Optional[BaseException] = None

    try:
        for n in precompleted:
            for s in successors(n, graph, state, completed, trigger_count):
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
                    push_node_failure_notification(
                        n,
                        exc,
                        state,
                        graph,
                        completed=completed,
                        running_names=list(running.values()),
                    )
                    continue
                try:
                    succs = successors(n, graph, state, completed, trigger_count)
                except _LlmPauseSignal as sig:
                    pause_edge = sig.edge
                    raise
                for s in succs:
                    spawn(s)
    except _LlmPauseSignal:
        await _cancel_running(running)
        push_llm_route_notification(pause_edge, state, graph)
        return LlmPauseResult(state=state, completed=completed, edge=pause_edge)
    except (GraphRecursionError, GraphRouterError) as e:
        await _cancel_running(running)
        fatal = e

    if fatal is not None:
        push_terminal_notification(
            graph, state, BgStatus.FAILED, error=fatal, initial_params=initial_params
        )
        raise fatal

    if all_errors:
        error = GraphBatchFailureError(all_errors)
        push_terminal_notification(
            graph, state, BgStatus.FAILED, error=error, initial_params=initial_params
        )
        raise error

    result = _collect_finish_result(graph, state)
    push_terminal_notification(
        graph, state, BgStatus.SUCCESS, result=result, initial_params=initial_params
    )
    return result


# ---------------------------------------------------------------------------
# compile → executor
# ---------------------------------------------------------------------------


def _build_executor(graph: Any):
    """Return an async ``executor(**initial_state) -> BgTaskResult``."""

    async def executor(**initial_state) -> BgTaskResult:
        state = graph.state_schema(**initial_state)
        entry = graph._get_entry_node()
        initial_params = dict(initial_state)
        return BgTaskResult(
            poll=_run_driver(
                graph,
                state,
                execute_nodes=[entry],
                completed=set(),
                trigger_count=defaultdict(set),
                initial_params=initial_params,
            ),
            command_name=graph.command_name,
            graph_ref=graph,
            initial_params=initial_params,
            factory=executor,
        )

    return executor


# ---------------------------------------------------------------------------
# Resume variants
# ---------------------------------------------------------------------------


def resume(graph: Any, state: Any, from_nodes: list[str]) -> BgTaskResult:
    """Resume execution by re-running *from_nodes*.

    Completed = nodes with a result that are not being re-run. The graph then
    transitions forward from *from_nodes*; AND-joins are prefilled so a merge
    that already had one source satisfied does not stall.
    """
    completed: set[str] = {
        name
        for name in graph._nodes
        if getattr(state, name, None) is not None and name not in from_nodes
    }
    for node_name in from_nodes:
        setattr(state, node_name, None)
        graph._nodes[node_name].status = BgStatus.PENDING

    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    return BgTaskResult(
        result=_MSG_RESUMING.format(from_node=", ".join(from_nodes)),
        poll=_run_driver(
            graph,
            state,
            execute_nodes=list(from_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
        ),
        command_name=graph.command_name,
        graph_ref=graph,
    )


def _apply_skip(graph: Any, state: Any, skip_nodes: list[str]) -> None:
    for sn in skip_nodes:
        node_def = graph._nodes[sn]
        existing = getattr(state, sn, None)
        if existing is None:
            setattr(state, sn, {})
        node_def.status = BgStatus.SKIPPED
        suffix = " with partial results" if existing is not None else ""
        report_progress(
            sn, BgStatus.SKIPPED, _MSG_SKIPPING.format(skip_nodes=sn, suffix=suffix)
        )


def resume_skip(graph: Any, state: Any, skip_nodes: list[str]) -> BgTaskResult:
    """Skip *skip_nodes* (keeping partial results) and continue downstream."""
    _apply_skip(graph, state, skip_nodes)
    completed: set[str] = {
        name
        for name in graph._nodes
        if getattr(state, name, None) is not None or name in skip_nodes
    }
    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    return BgTaskResult(
        result=_MSG_SKIPPING.format(skip_nodes=", ".join(skip_nodes), suffix=""),
        poll=_run_driver(
            graph,
            state,
            execute_nodes=[],
            precompleted=tuple(skip_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
        ),
        command_name=graph.command_name,
        graph_ref=graph,
    )


def resume_skip_and_from(
    graph: Any, state: Any, skip_nodes: list[str], from_nodes: list[str]
) -> BgTaskResult:
    """Skip *skip_nodes* then re-run *from_nodes*, continuing downstream."""
    _apply_skip(graph, state, skip_nodes)
    completed: set[str] = {
        name
        for name in graph._nodes
        if (getattr(state, name, None) is not None or name in skip_nodes)
        and name not in from_nodes
    }
    for node_name in from_nodes:
        setattr(state, node_name, None)
        graph._nodes[node_name].status = BgStatus.PENDING

    trigger_count: dict = defaultdict(set)
    _prefill_trigger_count(graph, completed, trigger_count)

    skip_desc = ", ".join(skip_nodes)
    from_desc = ", ".join(from_nodes)
    return BgTaskResult(
        result=f"Skipping [{skip_desc}], resuming from [{from_desc}]",
        poll=_run_driver(
            graph,
            state,
            execute_nodes=list(from_nodes),
            precompleted=tuple(skip_nodes),
            completed=completed,
            trigger_count=trigger_count,
            push_start=False,
        ),
        command_name=graph.command_name,
        graph_ref=graph,
    )
