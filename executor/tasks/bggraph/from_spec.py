"""Compile a declarative :class:`GraphSpec` into a runnable :class:`BgGraph`.

This is the bridge between the model-facing contract (:mod:`spec`) and the
execution engine (:mod:`engine`). Nothing here talks to the model or to a tool
directly: the caller injects a ``dispatch`` callback (``async (name, kwargs) ->
ToolResult``) that routes tool calls back through the executor chokepoint, so
permission gating / hooks / observability are preserved unchanged.

Three things happen:

1. **Bindings** (literal / ``$input`` / ``$ref``) are resolved against the live
   graph state at run time — see :func:`resolve_binding`.
2. **Edges** are derived automatically from the ``$ref`` data-flow (0 deps →
   ``START``, 1 → plain edge, ≥2 → AND-join), with explicit ``edges`` layered on
   top for branching (guarded ``when`` predicates → a conditional router) and
   manual ordering.
3. Each node becomes an async node fn returning a :class:`Stage`; its result is
   stored in state under the node's own id, so ``{"$ref": "<id>"}`` reads it.

The compiled graph runs **foreground** via :meth:`BgGraph.arun`, so approval /
AskUserQuestion prompts raised by dispatched tools surface on the live channel.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import datetime
import functools
import hashlib
import itertools
import json
import math
import re
import statistics
import string
import textwrap
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from asteval import Interpreter

from mote.executor.tasks.bggraph.graph import BgGraph
from mote.executor.tasks.bggraph.spec import GraphSpec, NodeSpec, Predicate, as_input_ref, as_node_ref
from mote.executor.tasks.bggraph.types import END, START, GraphState, Stage
from mote.executor.tool_result import ToolResult

# Injected by the caller (the ``run_graph`` tool): route a tool call back through
# the executor chokepoint. Returns the tool's ``ToolResult`` (never raises for a
# denied/failed call — failure is signalled by ``success=False``).
DispatchFn = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]

_MISSING = object()
_ELSE_KEY = "__else__"


class GraphToolError(RuntimeError):
    """A dispatched tool failed (or a binding could not resolve).

    A plain ``RuntimeError`` subclass so the engine's :class:`RecoveryRunner`
    classifies it as permanent (ABORT) — a failed/denied tool call is not
    transient, so the node fails fast without consuming the retry budget.
    """


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------


def _walk_path(base: Any, parts: list[str], *, where: str) -> Any:
    """Follow a dotted ref tail: dict key → list index → attribute."""
    for part in parts:
        if isinstance(base, dict):
            base = base[part]
        elif isinstance(base, (list, tuple)) and part.lstrip("-").isdigit():
            base = base[int(part)]
        else:
            base = getattr(base, part)
    return base


def resolve_binding(
    binding: Any,
    state: GraphState,
    env: dict[str, Any] | None = None,
    *,
    missing_ok: bool = False,
) -> Any:
    """Resolve a binding against *state* (and *env* for map loop variables).

    ``{"$input": f}`` / ``{"$ref": "n"}`` / ``{"$ref": "n.k"}`` resolve to values;
    dicts and lists are resolved element-wise; anything else is a literal. *env*
    (the ``{as_name: item}`` of a ``map`` body) shadows state for its head name.

    ``missing_ok`` controls the treatment of a ref to something absent: node args
    keep it ``False`` (an unresolved dep is a wiring bug → fail loudly), while
    output resolution sets it ``True`` so a ref into a branch that a conditional
    edge legitimately skipped yields ``None`` instead of crashing.
    """
    inp = as_input_ref(binding)
    if inp is not None:
        value = getattr(state, inp, _MISSING)
        if value is _MISSING:
            if missing_ok:
                return None
            raise GraphToolError(f"input {inp!r} is not available in the graph state")
        return value

    ref = as_node_ref(binding)
    if ref is not None:
        parts = ref.split(".")
        head, tail = parts[0], parts[1:]
        if env is not None and head in env:
            base = env[head]
        else:
            base = getattr(state, head, _MISSING)
            if base is _MISSING:
                if missing_ok:
                    return None
                raise GraphToolError(f"$ref {ref!r} points at {head!r}, which has not produced a value yet")
        try:
            return _walk_path(base, tail, where=ref)
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            if missing_ok:
                return None
            raise GraphToolError(f"$ref {ref!r} could not be resolved: {exc}") from exc

    if isinstance(binding, dict):
        return {k: resolve_binding(v, state, env, missing_ok=missing_ok) for k, v in binding.items()}
    if isinstance(binding, list):
        return [resolve_binding(v, state, env, missing_ok=missing_ok) for v in binding]
    return binding


# ---------------------------------------------------------------------------
# Predicate evaluation (conditional-edge routing)
# ---------------------------------------------------------------------------

_BINARY_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a,
}


def eval_predicate(pred: Predicate, state: GraphState) -> bool:
    left = resolve_binding(pred.left, state)
    if pred.op == "truthy":
        return bool(left)
    if pred.op == "falsy":
        return not bool(left)
    right = resolve_binding(pred.right, state)
    return _BINARY_OPS[pred.op](left, right)


# ---------------------------------------------------------------------------
# Node fns (tool / map / compute)
# ---------------------------------------------------------------------------


def _unwrap(result: ToolResult, node_id: str) -> Any:
    """Extract a dispatched tool's value, or fail the node on ``success=False``.

    Prefers structured ``data`` (so ``$ref`` can index into it) and falls back to
    the text ``output``. A denied/failed call raises :class:`GraphToolError`
    (permanent → node FAILED, no retry).
    """
    if not result.success:
        reason = result.error if result.error is not None else result.output
        raise GraphToolError(f"node {node_id!r}: tool call failed: {reason}")
    return result.data if result.data is not None else result.output


def _make_tool_node(node: NodeSpec, dispatch: DispatchFn) -> Callable:
    assert node.tool is not None  # guaranteed by NodeSpec validator for kind="tool"
    tool_name = node.tool
    node_id = node.id
    args = node.args

    async def fn(state: GraphState) -> Stage:
        async def submit() -> dict[str, Any]:
            kwargs = {k: resolve_binding(v, state) for k, v in args.items()}
            result = await dispatch(tool_name, kwargs)
            return {node_id: _unwrap(result, node_id)}

        return Stage(submit=submit(), name=node_id)

    return fn


def _make_map_node(node: NodeSpec, dispatch: DispatchFn) -> Callable:
    # tool + as_ are guaranteed by the NodeSpec validator for kind="map".
    assert node.tool is not None and node.as_ is not None
    tool_name = node.tool
    node_id = node.id
    as_name = node.as_
    over = node.over
    args = node.args
    concurrency = node.concurrency  # positive int; default 8 (bounded fan-out)

    async def fn(state: GraphState) -> Stage:
        items = resolve_binding(over, state)
        if not isinstance(items, (list, tuple)):
            raise GraphToolError(f"map node {node_id!r}: 'over' resolved to {type(items).__name__}, expected a list")

        # A semaphore caps how many items dispatch at once, so a large collection
        # does not launch every item's tool call simultaneously.
        sem = asyncio.Semaphore(concurrency)

        async def one(item: Any) -> Any:
            async with sem:
                env = {as_name: item}
                kwargs = {k: resolve_binding(v, state, env) for k, v in args.items()}
                result = await dispatch(tool_name, kwargs)
                return _unwrap(result, node_id)

        async def submit() -> dict[str, Any]:
            # gather raises on the first item failure → whole map node FAILED
            # (partial results are recoverable via resume, not silently dropped).
            # The semaphore caps in-flight items but results stay input-ordered.
            results = await asyncio.gather(*(one(it) for it in items))
            return {node_id: list(results)}

        return Stage(submit=submit(), name=node_id)

    return fn


# Pure, data-shaping stdlib modules seeded into every ``compute`` expression's
# scope. asteval forbids ``import``, so without this a compute node could not
# reach even ``re`` / ``json`` — which is what made string-munging (splitting a
# diff, parsing structured text) brittle. The set is deliberately I/O-free: no
# os / sys / subprocess / pathlib / socket / open, so compute stays a pure
# data-glue layer — it can parse, filter and reshape values, but cannot touch
# the filesystem, the network, or the executor chokepoint (a tool call goes
# through a ``tool``/``map`` node, never here, so there is exactly one governed
# control plane). Extend only with modules that are pure value transforms.
_COMPUTE_NAMESPACE: dict[str, Any] = {
    "re": re,
    "json": json,
    "math": math,
    "statistics": statistics,
    "itertools": itertools,
    "functools": functools,
    "collections": collections,
    "textwrap": textwrap,
    "string": string,
    "datetime": datetime,
    "base64": base64,
    "hashlib": hashlib,
}

# asteval defaults are NOT tight enough for a pure-glue guarantee: it ships
# ``open`` in its symtable (real filesystem I/O) and, with ``use_numpy=True``
# (its default), ~300 numpy builtins including file readers (``fromfile`` /
# ``loadtxt`` / ``genfromtxt`` / ``fromregex`` / ``frombuffer``). To make
# "compute is a pure value transform" a *construction* rather than a convention
# we build the interpreter with numpy off and language features that have no
# place in glue disabled, then pop the residual I/O / introspection builtins.
#   - config: import/importfrom are already blocked by asteval, we assert it
#     explicitly; ``while`` is dropped so an expression cannot spin an unbounded
#     loop (comprehensions / ``for`` over finite data still work).
#   - banned builtins: file I/O (``open`` + the numpy readers, defensive even
#     with use_numpy=False) and interactive / introspection surface
#     (``input`` / ``print`` / ``dir`` / ``id`` / ``type``).
_COMPUTE_CONFIG: dict[str, bool] = {"import": False, "importfrom": False, "while": False}
_COMPUTE_BANNED = frozenset(
    {"open", "input", "print", "dir", "id", "type", "fromfile", "loadtxt", "genfromtxt", "fromregex", "frombuffer"}
)
# Hard wall-clock ceiling for a single compute expression. asteval has NO
# built-in time / iteration limit and runs in-process, so a runaway (or merely
# huge) expression would otherwise burn a core with no way out. We evaluate on a
# worker thread under ``wait_for`` so the event loop stays responsive and the
# node fails cleanly on timeout. (A thread cannot be force-killed, so a runaway
# keeps running to completion in the background — true hard-kill needs a
# subprocess, which is what Bash is for; this ceiling bounds the *agent's* wait,
# not the thread's CPU.) 5 minutes: generous for legitimate data shaping.
_COMPUTE_TIMEOUT_S = 300.0


def _eval_expr(expr: str, symbols: dict[str, Any], node_id: str) -> Any:
    """Evaluate a compute expression under a locked-down asteval interpreter.

    The interpreter is built with numpy off and :data:`_COMPUTE_CONFIG` (no
    ``import`` / ``importfrom`` / ``while``), then :data:`_COMPUTE_BANNED` names
    (``open`` + numpy file readers + introspection builtins) are removed from the
    symtable — so compute has no filesystem / network / import / tool surface by
    *construction*, not convention. It is then seeded with
    :data:`_COMPUTE_NAMESPACE` (pure, I/O-free stdlib modules) so an expression
    can parse / filter / reshape data; the node's resolved ``args`` overlay that
    namespace last, so a matching arg name shadows a module if the model wants.
    """
    interp = Interpreter(use_numpy=False, config=_COMPUTE_CONFIG)
    for name in _COMPUTE_BANNED:
        interp.symtable.pop(name, None)
    interp.symtable.update(_COMPUTE_NAMESPACE)
    interp.symtable.update(symbols)
    try:
        return interp.eval(expr, raise_errors=True)
    except Exception as exc:  # noqa: BLE001 — surface any eval failure as a node failure
        raise GraphToolError(f"compute node {node_id!r}: expression failed: {exc}") from exc


def _make_compute_node(node: NodeSpec) -> Callable:
    assert node.expr is not None  # guaranteed by NodeSpec validator for kind="compute"
    node_id = node.id
    expr = node.expr
    args = node.args

    async def fn(state: GraphState) -> Stage:
        async def submit() -> dict[str, Any]:
            symbols = {k: resolve_binding(v, state) for k, v in args.items()}
            # Run the (synchronous, potentially slow) eval off the event loop and
            # under a wall-clock ceiling: the loop stays responsive and a runaway
            # expression fails the node instead of freezing the whole agent.
            try:
                value = await asyncio.wait_for(
                    asyncio.to_thread(_eval_expr, expr, symbols, node_id),
                    _COMPUTE_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                # Convert to GraphToolError so the runner treats it as a hard node
                # failure (ABORT), not a transient error worth retrying.
                raise GraphToolError(f"compute node {node_id!r}: exceeded {_COMPUTE_TIMEOUT_S:g}s time limit") from exc
            return {node_id: value}

        return Stage(submit=submit(), name=node_id)

    return fn


# ---------------------------------------------------------------------------
# Ref discovery (for auto-edges and compile-time validation)
# ---------------------------------------------------------------------------


def _iter_refs(binding: Any) -> Iterator[tuple[str, str]]:
    """Yield ``("input", name)`` / ``("ref", path)`` leaves inside a binding."""
    inp = as_input_ref(binding)
    if inp is not None:
        yield ("input", inp)
        return
    ref = as_node_ref(binding)
    if ref is not None:
        yield ("ref", ref)
        return
    if isinstance(binding, dict):
        for v in binding.values():
            yield from _iter_refs(v)
    elif isinstance(binding, list):
        for v in binding:
            yield from _iter_refs(v)


def _node_bindings(node: NodeSpec) -> Iterator[Any]:
    """All bindings a node consumes (args, plus ``over`` for a map)."""
    yield from node.args.values()
    if node.kind == "map":
        yield node.over


def _node_deps(node: NodeSpec, node_ids: set[str]) -> list[str]:
    """Upstream node ids this node depends on (via ``$ref``), order-preserved.

    A map node's own loop variable (``as``) is *not* a dependency — it names the
    per-item value, not another node.
    """
    local = {node.as_} if node.kind == "map" else set()
    deps: list[str] = []
    for binding in _node_bindings(node):
        for kind, name in _iter_refs(binding):
            if kind != "ref":
                continue
            head = name.split(".")[0]
            if head in node_ids and head != node.id and head not in local and head not in deps:
                deps.append(head)
    return deps


# ---------------------------------------------------------------------------
# Compile-time ref validation
# ---------------------------------------------------------------------------


def _validate_refs(spec: GraphSpec) -> None:
    """Check every ``$ref``/``$input`` points at something that exists.

    ``spec.py`` only enforces binding *shape*; this needs the full node/input set,
    so it lives in the compiler.
    """
    input_names = set(spec.inputs)
    node_ids = {n.id for n in spec.nodes}

    def check(binding: Any, *, where: str, local: set[str]) -> None:
        for kind, name in _iter_refs(binding):
            if kind == "input":
                if name not in input_names:
                    raise ValueError(f"{where}: $input {name!r} is not a declared graph input")
            else:  # ref
                head = name.split(".")[0]
                if head not in node_ids and head not in local:
                    raise ValueError(f"{where}: $ref {name!r} points at unknown node {head!r}")

    collision = node_ids & input_names
    if collision:
        raise ValueError(f"node id(s) collide with input name(s): {sorted(collision)}")

    for node in spec.nodes:
        local: set[str] = {node.as_} if node.kind == "map" and node.as_ else set()
        for key, binding in node.args.items():
            check(binding, where=f"node[{node.id}].args.{key}", local=local)
        if node.kind == "map":
            check(node.over, where=f"node[{node.id}].over", local=set())

    for edge in spec.edges:
        if edge.when is not None:
            check(edge.when.left, where=f"edge {edge.from_}->{edge.to} .when.left", local=set())
            if edge.when.right is not None:
                check(edge.when.right, where=f"edge {edge.from_}->{edge.to} .when.right", local=set())

    check(spec.output, where="output", local=set())


# ---------------------------------------------------------------------------
# Edge wiring
# ---------------------------------------------------------------------------


def _make_router(edges: list, else_key: str) -> Callable[[GraphState], str]:
    """Build a conditional router: first edge whose predicate holds, else *else_key*."""

    def router(state: GraphState) -> str:
        for idx, edge in enumerate(edges):
            if eval_predicate(edge.when, state):
                return str(idx)
        return else_key

    return router


def _wire_edges(graph: BgGraph, spec: GraphSpec) -> None:
    node_ids = {n.id for n in spec.nodes}

    # Group explicit edges: guarded (branch) vs unguarded (ordering / else).
    guarded: dict[str, list] = defaultdict(list)
    unguarded: dict[str, list[str]] = defaultdict(list)
    for edge in spec.edges:
        if edge.when is not None:
            guarded[edge.from_].append(edge)
        else:
            unguarded[edge.from_].append(edge.to)
    conditional_sources = set(guarded)
    explicit_pairs = {(e.from_, e.to) for e in spec.edges}

    # 1. Auto data-flow edges (incoming). A dep whose source is a branch source
    #    is routed by that source's conditional edge instead, so it is skipped
    #    here; an already-explicit pair is likewise left to the explicit edge.
    for node in spec.nodes:
        srcs = [
            d for d in _node_deps(node, node_ids) if d not in conditional_sources and (d, node.id) not in explicit_pairs
        ]
        if len(srcs) == 1:
            graph.add_edge(srcs[0], node.id)
        elif len(srcs) >= 2:
            graph.add_edge(srcs, node.id)  # AND-join

    # 2. Conditional edges from guarded groups (first-match wins; else fallthrough).
    for src, edges in guarded.items():
        mapping = {str(i): e.to for i, e in enumerate(edges)}
        else_targets = unguarded.get(src, [])
        mapping[_ELSE_KEY] = else_targets[0] if else_targets else END
        graph.add_conditional_edges(src, _make_router(edges, _ELSE_KEY), mapping)

    # 3. Plain explicit edges from non-branch sources (a branch source's single
    #    unguarded edge is already consumed as its else target above).
    for edge in spec.edges:
        if edge.when is None and edge.from_ not in conditional_sources:
            graph.add_edge(edge.from_, edge.to)

    # 4. START / END fill-in: any node with no incoming edge is an entry point;
    #    any with no outgoing edge is a finish node.
    targets: set[str] = set()
    sources: set[str] = set()
    for e in graph._edges:
        targets.add(e.to_node)
        sources.add(e.from_node)
    for we in graph._waiting_edges:
        targets.add(we.to_node)
        sources.update(we.sources)
    for ce in graph._conditional_edges:
        sources.add(ce.from_node)
        targets.update(ce.mapping.values())
    for nid in (n.id for n in spec.nodes):
        if nid not in targets:
            graph.add_edge(START, nid)
        if nid not in sources:
            graph.add_edge(nid, END)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_graph(
    spec: GraphSpec,
    *,
    dispatch: DispatchFn,
    command_name: str = "run_graph",
    valid_tools: set[str] | None = None,
    recursion_limit: int = 100,
) -> BgGraph:
    """Compile *spec* into an (uncompiled) :class:`BgGraph`.

    Args:
        dispatch: routes a tool call back through the executor chokepoint.
        valid_tools: if given, tool/map node tool names are checked against it at
            compile time (a fast, clear failure instead of a per-node runtime one).
        recursion_limit: total-node-activation bound (guards runaway cycles).

    The returned graph runs foreground via :meth:`BgGraph.arun`.
    """
    _validate_refs(spec)

    if valid_tools is not None:
        for node in spec.nodes:
            if node.kind in ("tool", "map") and node.tool not in valid_tools:
                raise ValueError(f"node {node.id!r} references unknown tool {node.tool!r}")

    graph = BgGraph(command_name=command_name, recursion_limit=recursion_limit)

    for node in spec.nodes:
        if node.kind == "tool":
            fn = _make_tool_node(node, dispatch)
        elif node.kind == "map":
            fn = _make_map_node(node, dispatch)
        else:  # compute
            fn = _make_compute_node(node)
        graph.add_node(node.id, fn, params={})

    _wire_edges(graph, spec)
    return graph


def resolve_output(spec: GraphSpec, state: GraphState) -> Any:
    """Resolve the spec's ``output`` binding tree against the final *state*.

    Lenient on missing refs (``missing_ok=True``): a ref into a branch a
    conditional edge skipped yields ``None`` rather than raising.
    """
    return resolve_binding(spec.output, state, missing_ok=True)
