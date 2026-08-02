"""Compile a declarative :class:`GraphSpec` into a runnable :class:`WorkflowBuilder`.

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

The compiled graph runs **foreground** via :meth:`WorkflowBuilder.arun`, so approval /
AskUserQuestion prompts raised by dispatched tools surface on the live channel.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextvars
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, create_model

try:
    from asteval import Interpreter  # type: ignore[reportMissingImports] - optional Product backend
except ImportError:  # Optional RunGraph compute backend; checked at activation.
    Interpreter = None  # type: ignore[assignment,misc]

from mote.contracts.workflow.definition_source import DeclarativeWorkflowDefinitionSource
from mote.contracts.workflow.execution import WorkflowNodeDispatchResult
from mote.orchestration.workflows import NoOutput
from mote.orchestration.workflows.graph import WorkflowBuilder
from mote.orchestration.workflows.types import END, START, GraphState, Stage
from mote.product.workflows.run_graph.spec import (
    FMT_KEY,
    ChannelSpec,
    GraphSpec,
    NodeSpec,
    Predicate,
    as_fmt_template,
    as_input_ref,
    as_node_ref,
)
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.tool_result import ToolResult

# Injected by the caller (the ``run_graph`` tool): route a tool call back through
# the executor chokepoint. Returns the tool's ``ToolResult`` (never raises for a
# denied/failed call — failure is signalled by ``success=False``).
DispatchResult = ToolResult | WorkflowNodeDispatchResult
DispatchFn = Callable[[str, dict[str, Any]], Awaitable[DispatchResult]]

_MISSING = object()
_ELSE_KEY = "__else__"

# Sentinel marking a map item whose tool call failed under ``on_item_error="skip"``.
# Such items contribute nothing: they are filtered out of the result list, so a
# skipped item is the identity element of "collect into a list" (its fold twin,
# in ``_make_fold_node``, simply does not fold the failed value into the acc).
_SKIP = object()


# ---------------------------------------------------------------------------
# Failure sink — surface per-item map/fold failures (with their args) to the model
# ---------------------------------------------------------------------------
#
# A per-item failure is silent to the model unless we carry it out: the
# foreground ``arun`` path installs no progress writer (only the background pool
# does), so the per-item ``logger.warning`` reaches operators but never the
# agent — and a hard failure only reaches it as the terse "Nodes failed: <id>".
# Neither carries the *resolved input args*, so the model can't reproduce the
# call to retry it. We collect failures through a ContextVar sink — the SAME
# mechanism ``report_progress`` uses — recording each failed item's tool name +
# resolved kwargs so the model can retry that exact call. It works without
# threading state through every node signature and without polluting the
# field/channel state model (parallel map items would clobber a shared key).
# ``run_graph`` opens :func:`collect_item_failures` around ``arun`` and folds the
# gathered list into the tool result's output text.
#
# The sink is a plain list read (not re-``set``) inside node coroutines, so the
# child tasks ``asyncio.create_task`` spawns share the SAME list the caller
# installed (contextvars copy the reference on task creation) — mirroring how
# the progress writer reaches node coroutines.


@dataclass
class ItemFailure:
    """One map/fold item whose tool call failed.

    Carries the resolved ``args`` (the exact kwargs dispatched) so the model can
    retry the call. ``skipped`` distinguishes ``on_item_error="skip"`` (dropped,
    batch continued) from a fatal failure that sank the node.
    """

    node: str
    tool: str
    args: dict[str, Any]  # resolved kwargs — the exact call to retry
    error: str
    skipped: bool


_failure_sink: contextvars.ContextVar[list[ItemFailure] | None] = contextvars.ContextVar(
    "_bggraph_failure_sink", default=None
)


def record_item_failure(
    node_id: str,
    tool: str,
    args: dict[str, Any],
    error: BaseException,
    *,
    skipped: bool,
) -> None:
    """Record (and log) one failed map/fold item, with its resolved args.

    Always logs for operators; also appends to the active failure sink (if any is
    installed via :func:`collect_item_failures`) so the model learns of the
    failure — and the exact args to retry — through the tool result. No-op on the
    sink outside a scope. The stored ``error`` is the bare reason (the ``_unwrap``
    ``node 'x': tool call failed:`` boilerplate is stripped, since the note names
    the node once itself).
    """
    logger.warning(f"{node_id!r}: {'skipping' if skipped else 'failing on'} item: {error}")
    sink = _failure_sink.get()
    if sink is not None:
        prefix = f"node {node_id!r}: tool call failed: "
        reason = str(error)
        reason = reason[len(prefix) :] if reason.startswith(prefix) else reason
        sink.append(ItemFailure(node=node_id, tool=tool, args=args, error=reason, skipped=skipped))


@contextmanager
def collect_item_failures() -> Iterator[list[ItemFailure]]:
    """Install a fresh failure sink for the duration of a run; yield the list.

    The yielded list is populated (in call order across serial folds; in
    completion order across a map's concurrent items) as items fail. Restores the
    previous sink on exit so nested runs don't cross-talk.
    """
    sink: list[ItemFailure] = []
    token = _failure_sink.set(sink)
    try:
        yield sink
    finally:
        _failure_sink.reset(token)


class GraphToolError(RuntimeError):
    """A dispatched tool failed (or a binding could not resolve).

    A plain ``RuntimeError`` subclass so the engine's :class:`RecoveryRunner`
    classifies it as permanent (ABORT) — a failed/denied tool call is not
    transient, so the node fails fast without consuming the retry budget.
    """


# ---------------------------------------------------------------------------
# Channels — loop-carried state (reducers + dynamic state schema)
# ---------------------------------------------------------------------------
#
# A ``ChannelSpec`` declares a mutable state cell with an initial value and a
# reducer. We compile the channels into a pydantic ``GraphState`` subclass whose
# fields carry the reducer in their ``Annotated`` metadata, so the engine's
# existing ``derive_reducers`` / ``apply_updates`` machinery folds repeated
# writes exactly as it does for a hand-written graph's state — no second state
# model. The reducer vocabulary is a FIXED, named set (never arbitrary code), so
# a channel's merge semantics stay declarative, safe, and stable over time.
#
# Every reducer has the langgraph shape ``(current, update) -> merged`` with
# exactly two positional params (so ``channels._is_reducer`` recognises it) and
# treats ``current is None`` as the identity element, so a fold works even
# before the channel's first real write.


def _reduce_append(current: Any, update: Any) -> list:
    return (current if current is not None else []) + [update]


def _reduce_extend(current: Any, update: Any) -> list:
    return (current if current is not None else []) + list(update)


def _reduce_add(current: Any, update: Any) -> Any:
    return update if current is None else current + update


def _reduce_or(current: Any, update: Any) -> bool:
    return bool(current) or bool(update)


def _reduce_and(current: Any, update: Any) -> bool:
    return bool(current) and bool(update)


def _reduce_min(current: Any, update: Any) -> Any:
    return update if current is None else min(current, update)


def _reduce_max(current: Any, update: Any) -> Any:
    return update if current is None else max(current, update)


def _reduce_merge(current: Any, update: Any) -> dict:
    return {**(current if current is not None else {}), **update}


# Named reducers only (no ``operator.add`` — its C-level signature is not
# introspectable, so ``channels._is_reducer`` would reject it). ``last`` has no
# entry: a last-value channel needs no reducer (plain ``setattr`` overwrite), so
# it is compiled as an un-annotated field.
_REDUCE_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "append": _reduce_append,
    "extend": _reduce_extend,
    "add": _reduce_add,
    "or": _reduce_or,
    "and": _reduce_and,
    "min": _reduce_min,
    "max": _reduce_max,
    "merge": _reduce_merge,
}

# ``ChannelSpec.type`` (JSON-schema-ish literal) → python annotation. Advisory
# only: ``GraphState`` does not validate on assignment (``extra="allow"``, no
# ``validate_assignment``), so a channel's declared type documents intent and
# feeds schema introspection without policing node writes at run time.
_CHANNEL_PYTYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "list": list,
    "object": dict,
    "any": Any,
}


def _build_state_schema(spec: GraphSpec) -> type[GraphState]:
    """Compile the spec's channels into a :class:`GraphState` subclass.

    Each channel becomes a state field defaulting to a deep copy of its
    ``initial`` (so mutable initials are not shared across runs) and, unless it
    is a last-value channel, carrying its reducer in ``Annotated`` metadata for
    :func:`channels.derive_reducers`. Node results are *not* declared — they ride
    ``extra="allow"``.

    Declared **optional** inputs (``required=False``) are materialised with a
    ``None`` default so an ``{"$input": x}`` reference to one the caller omitted
    resolves to ``None`` instead of raising — honouring the ``required`` flag,
    which is otherwise inert. **Required** inputs are deliberately left to ride
    ``extra="allow"`` so a missing one still fails loudly (``GraphToolError:
    input 'x' is not available``) at first reference.
    """
    fields: dict[str, Any] = {}
    for name, ch in spec.channels.items():
        assert isinstance(ch, ChannelSpec)  # narrows for the type checker
        pytype = _CHANNEL_PYTYPES.get(ch.type, Any)
        if ch.reduce == "last":
            annotation: Any = pytype
        else:
            annotation = Annotated[pytype, _REDUCE_OPS[ch.reduce]]
        # deepcopy per-instance via default_factory so a list/dict initial is
        # never shared between concurrent or resumed runs.
        fields[name] = (
            annotation,
            Field(default_factory=lambda v=ch.initial: deepcopy(v)),
        )

    # Seed omitted optional inputs to None. Untyped (Any) to match how inputs
    # otherwise ride extra="allow" — provided values are never coerced. Channel
    # names never collide (spec._check enforces node/input/channel disjointness),
    # but guard anyway so a channel default always wins.
    for name, field in spec.inputs.items():
        if not field.required and name not in fields:
            fields[name] = (Any, None)

    if not fields:
        return GraphState

    return create_model("RunGraphState", __base__=GraphState, **fields)


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

    template = as_fmt_template(binding)
    if template is not None:
        # Resolve each sibling fill value (itself a binding), then format. A
        # missing/bad placeholder becomes a clear node failure rather than a raw
        # KeyError/IndexError from str.format.
        fills = {k: resolve_binding(v, state, env, missing_ok=missing_ok) for k, v in binding.items() if k != FMT_KEY}
        try:
            return template.format(**fills)
        except (KeyError, IndexError, AttributeError) as exc:
            if missing_ok:
                return None
            raise GraphToolError(f"$fmt template {template!r} could not be formatted: {exc}") from exc

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


def _unwrap(result: DispatchResult, node_id: str) -> Any:
    """Extract a dispatched tool's value, or fail the node on ``success=False``.

    Prefers the canonical durable payload (so ``$ref`` can index into it) and falls back to
    the text ``output``. A denied/failed call raises :class:`GraphToolError`
    (permanent → node FAILED, no retry).
    """
    if not result.success:
        reason = result.error if result.error is not None else result.output
        raise GraphToolError(f"node {node_id!r}: tool call failed: {reason}")
    return result.payload.materialize() if result.payload is not None else result.output


def _sink(node: NodeSpec) -> str:
    """State key a node's produced value is stored under.

    Default is the node's own id (single-assignment result slot); if the node
    declares ``writes``, its value is merged into that channel instead (through
    the channel's reducer, applied by the engine).
    """
    return node.writes if node.writes is not None else node.id


def _make_tool_node(node: NodeSpec, dispatch: DispatchFn) -> Callable:
    assert node.tool is not None  # guaranteed by NodeSpec validator for kind="tool"
    tool_name = node.tool
    node_id = node.id
    sink = _sink(node)
    args = node.args

    async def fn(state: GraphState) -> Stage:
        async def submit() -> dict[str, Any]:
            kwargs = {k: resolve_binding(v, state) for k, v in args.items()}
            result = await dispatch(tool_name, kwargs)
            return {sink: _unwrap(result, node_id)}

        return Stage(submit=submit(), name=node_id)

    return fn


def _make_map_node(node: NodeSpec, dispatch: DispatchFn) -> Callable:
    # tool + as_ are guaranteed by the NodeSpec validator for kind="map".
    assert node.tool is not None and node.as_ is not None
    tool_name = node.tool
    node_id = node.id
    sink = _sink(node)
    as_name = node.as_
    over = node.over
    args = node.args
    concurrency = node.concurrency  # positive int; default 8 (bounded fan-out)
    # map defaults to "skip" (independent items — one failure must not discard the
    # successful rest); an explicit on_item_error overrides.
    skip_on_error = node.effective_on_item_error == "skip"

    async def fn(state: GraphState) -> Stage:
        items = resolve_binding(over, state)
        if not isinstance(items, (list, tuple)):
            raise GraphToolError(f"map node {node_id!r}: 'over' resolved to {type(items).__name__}, expected a list")

        # A semaphore caps how many items dispatch at once, so a large collection
        # does not launch every item's tool call simultaneously.
        sem = asyncio.Semaphore(concurrency)

        async def one(item: Any) -> Any:
            # Resolve the per-item kwargs OUTSIDE the try so a failed dispatch can
            # record the exact call (tool + resolved args) for the model to retry.
            async with sem:
                env = {as_name: item}
                kwargs = {k: resolve_binding(v, state, env) for k, v in args.items()}
                try:
                    result = await dispatch(tool_name, kwargs)
                    return _unwrap(result, node_id)
                except Exception as exc:  # noqa: BLE001 — record args then apply the policy
                    # Either isolate this item (skip → drop from the batch) or let it
                    # sink the whole node (fail); either way the model gets the tool +
                    # resolved args + reason so it can retry the exact call. Never
                    # ``BaseException``: a ``CancelledError`` must still cancel gather.
                    record_item_failure(node_id, tool_name, kwargs, exc, skipped=skip_on_error)
                    if not skip_on_error:
                        raise
                    return _SKIP

        async def submit() -> dict[str, Any]:
            # fail mode: gather raises on the first item failure → whole map node
            # FAILED (partial results are recoverable via resume, not silently
            # dropped). skip mode: each item is isolated, failed items drop out of
            # the result list. Either way the semaphore caps in-flight items and
            # results stay input-ordered.
            results = await asyncio.gather(*(one(it) for it in items))
            if skip_on_error:
                kept = [r for r in results if r is not _SKIP]
                # All-failed guard: isolating a single bad item is the point, but a
                # NON-EMPTY input yielding ZERO survivors is a systematic failure
                # (e.g. a wrong argument fails every call) — surfacing that as an
                # empty result would silently feed garbage downstream. Fail loudly
                # instead, so "one bad item → isolate" and "everything broke → fail"
                # stay distinct. (An empty input legitimately yields an empty list.)
                if items and not kept:
                    raise GraphToolError(
                        f"map node {node_id!r}: all {len(items)} item(s) failed under "
                        f'on_item_error="skip" — no result survived (a systematic failure, '
                        f"not an isolated one)"
                    )
                results = kept
            return {sink: list(results)}

        return Stage(submit=submit(), name=node_id)

    return fn


def _make_fold_node(node: NodeSpec, dispatch: DispatchFn) -> Callable:
    """map's *serial* twin: iterate a tool over a collection, threading an accumulator.

    Where ``map`` fans every item out concurrently and collects a list, ``fold``
    walks the collection one item at a time, exposing both the current item
    (``as``) and the running accumulator (``acc``) to the tool body, then folds
    each result into the accumulator via the same named ``reduce`` reducer a
    channel uses. Its produced value is the *final* accumulator — so an
    order-dependent iteration (each step reads what earlier steps built) is one
    declarative node instead of a hand-wired channel + back-edge loop.
    """
    # tool + as_ + acc are guaranteed by the NodeSpec validator for kind="fold".
    assert node.tool is not None and node.as_ is not None and node.acc is not None
    tool_name = node.tool
    node_id = node.id
    sink = _sink(node)
    as_name = node.as_
    acc_name = node.acc
    over = node.over
    args = node.args
    initial = node.initial
    # ``last`` (no reducer) means each item's result replaces the accumulator, so
    # the final value is the last item's — mirroring a last-value channel.
    reduce_fn = _REDUCE_OPS.get(node.reduce)
    # fold defaults to "fail" (dependent items — a skipped item breaks the chain
    # the later ones read); an explicit on_item_error overrides.
    skip_on_error = node.effective_on_item_error == "skip"

    async def fn(state: GraphState) -> Stage:
        items = resolve_binding(over, state)
        if not isinstance(items, (list, tuple)):
            raise GraphToolError(f"fold node {node_id!r}: 'over' resolved to {type(items).__name__}, expected a list")

        async def submit() -> dict[str, Any]:
            # deepcopy so a mutable initial is never shared across laps (this node
            # may re-run under a back-edge) or between concurrent runs.
            acc = deepcopy(initial)
            for item in items:
                # Both the item and the running accumulator shadow state for the
                # body's refs.
                env = {as_name: item, acc_name: acc}
                kwargs = {k: resolve_binding(v, state, env) for k, v in args.items()}
                try:
                    result = await dispatch(tool_name, kwargs)
                    value = _unwrap(result, node_id)
                except Exception as exc:  # noqa: BLE001 — see on_item_error handling
                    # fail mode: propagate → whole fold node FAILED. skip mode: the
                    # failed item is not folded into the accumulator (its identity
                    # element) and the iteration continues. Either way the model gets
                    # the tool + resolved args (incl. the acc it saw) + reason so it
                    # can retry the exact call. ``BaseException`` (CancelledError) is
                    # never caught, so a cancelled fold still cancels.
                    record_item_failure(node_id, tool_name, kwargs, exc, skipped=skip_on_error)
                    if not skip_on_error:
                        raise
                    continue
                acc = value if reduce_fn is None else reduce_fn(acc, value)
            return {sink: acc}

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
_COMPUTE_CONFIG: dict[str, bool] = {
    "import": False,
    "importfrom": False,
    "while": False,
}
_COMPUTE_BANNED = frozenset(
    {
        "open",
        "input",
        "print",
        "dir",
        "id",
        "type",
        "fromfile",
        "loadtxt",
        "genfromtxt",
        "fromregex",
        "frombuffer",
    }
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
    if Interpreter is None:
        raise RuntimeError("RunGraph compute expressions require the optional 'asteval' dependency")
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
    sink = _sink(node)
    expr = node.expr
    args = node.args

    async def fn(state: GraphState) -> Stage:
        async def submit() -> dict[str, Any]:
            symbols = {k: resolve_binding(v, state) for k, v in args.items()}
            # Run the (synchronous, potentially slow) eval off the event loop and
            # under a wall-clock ceiling: the loop stays responsive and a runaway
            # expression fails the node instead of freezing the whole agent.
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mote-graph-compute")
            try:
                loop = asyncio.get_running_loop()
                value = await asyncio.wait_for(
                    loop.run_in_executor(executor, _eval_expr, expr, symbols, node_id),
                    _COMPUTE_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                # Convert to GraphToolError so the runner treats it as a hard node
                # failure (ABORT), not a transient error worth retrying.
                raise GraphToolError(f"compute node {node_id!r}: exceeded {_COMPUTE_TIMEOUT_S:g}s time limit") from exc
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            return {sink: value}

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
    template = as_fmt_template(binding)
    if template is not None:
        # Refs live only in the sibling fill values; the template string itself is
        # plain text (its ``{name}`` placeholders are matched to siblings, not refs).
        for key, v in binding.items():
            if key != FMT_KEY:
                yield from _iter_refs(v)
        return
    if isinstance(binding, dict):
        for v in binding.values():
            yield from _iter_refs(v)
    elif isinstance(binding, list):
        for v in binding:
            yield from _iter_refs(v)


def _node_bindings(node: NodeSpec) -> Iterator[Any]:
    """All bindings a node consumes (args, plus ``over`` for a map/fold)."""
    yield from node.args.values()
    if node.kind in ("map", "fold"):
        yield node.over


def _local_names(node: NodeSpec) -> set[str]:
    """Loop-local ref heads a node introduces (not dependencies on other nodes).

    A map body's item (``as``); a fold body's item (``as``) *and* accumulator
    (``acc``). These name per-iteration values, not another node's result, so a
    ``$ref`` to them must not create a data-flow edge.
    """
    if node.kind == "map":
        return {n for n in (node.as_,) if n}
    if node.kind == "fold":
        return {n for n in (node.as_, node.acc) if n}
    return set()


def _node_deps(node: NodeSpec, node_ids: set[str]) -> list[str]:
    """Upstream node ids this node depends on (via ``$ref``), order-preserved.

    A map/fold node's own loop variables (``as``, and ``acc`` for fold) are *not*
    dependencies — they name per-item / running values, not another node.
    """
    local = _local_names(node)
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
    channel_names = set(spec.channels)
    # A ``$ref`` head may name a node result OR a channel; both live in the
    # single state object, differing only in edge semantics (a channel ref adds
    # no data-flow edge — see ``_node_deps``).
    ref_heads = node_ids | channel_names

    def check(binding: Any, *, where: str, local: set[str]) -> None:
        for kind, name in _iter_refs(binding):
            if kind == "input":
                if name not in input_names:
                    raise ValueError(f"{where}: $input {name!r} is not a declared graph input")
            else:  # ref
                head = name.split(".")[0]
                if head not in ref_heads and head not in local:
                    raise ValueError(f"{where}: $ref {name!r} points at unknown node/channel {head!r}")

    collision = node_ids & input_names
    if collision:
        raise ValueError(f"node id(s) collide with input name(s): {sorted(collision)}")

    for node in spec.nodes:
        local = _local_names(node)
        for key, binding in node.args.items():
            check(binding, where=f"node[{node.id}].args.{key}", local=local)
        if node.kind in ("map", "fold"):
            check(node.over, where=f"node[{node.id}].over", local=set())

    for edge in spec.edges:
        if edge.when is not None:
            check(
                edge.when.left,
                where=f"edge {edge.from_}->{edge.to} .when.left",
                local=set(),
            )
            if edge.when.right is not None:
                check(
                    edge.when.right,
                    where=f"edge {edge.from_}->{edge.to} .when.right",
                    local=set(),
                )

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


def _wire_edges(graph: WorkflowBuilder, spec: GraphSpec) -> None:
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
        edge_payload = json.dumps(
            [edge.model_dump(mode="json") for edge in edges],
            sort_keys=True,
            separators=(",", ":"),
        )
        graph.add_conditional_edges(
            src,
            _make_router(edges, _ELSE_KEY),
            mapping,
            implementation_id=(
                "mote.product.run-graph-router.v1.sha256-" f"{hashlib.sha256(edge_payload.encode()).hexdigest()}"
            ),
        )

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
) -> WorkflowBuilder:
    """Compile *spec* into an (uncompiled) :class:`WorkflowBuilder`.

    Args:
        dispatch: routes a tool call back through the executor chokepoint.
        valid_tools: if given, tool/map node tool names are checked against it at
            compile time (a fast, clear failure instead of a per-node runtime one).
        recursion_limit: total-node-activation bound (guards runaway cycles). The
            spec's own ``recursion_limit`` (when set) takes precedence, so the
            model can raise the loop budget for a legitimately long iteration.

    The returned graph runs foreground via :meth:`WorkflowBuilder.arun`.
    """
    _validate_refs(spec)

    if valid_tools is not None:
        for node in spec.nodes:
            if node.kind in ("tool", "map", "fold") and node.tool not in valid_tools:
                raise ValueError(f"node {node.id!r} references unknown tool {node.tool!r}")

    limit = spec.recursion_limit if spec.recursion_limit is not None else recursion_limit
    graph = WorkflowBuilder(
        command_name=command_name,
        state_schema=_build_state_schema(spec),
        recursion_limit=limit,
        output=NoOutput,
    )
    source_payload = json.dumps(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    graph.bind_definition_source(
        DeclarativeWorkflowDefinitionSource(
            "mote.product.run-graph",
            1,
            source_payload,
            hashlib.sha256(source_payload.encode()).hexdigest(),
        )
    )

    for node in spec.nodes:
        if node.kind == "tool":
            fn = _make_tool_node(node, dispatch)
        elif node.kind == "map":
            fn = _make_map_node(node, dispatch)
        elif node.kind == "fold":
            fn = _make_fold_node(node, dispatch)
        else:  # compute
            fn = _make_compute_node(node)
        node_payload = json.dumps(
            node.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        graph.add_node(
            node.id,
            fn,
            params={},
            implementation_id=(
                f"mote.product.run-graph-node.v1.{node.kind}.sha256-"
                f"{hashlib.sha256(node_payload.encode()).hexdigest()}"
            ),
        )

    _wire_edges(graph, spec)
    return graph


def resolve_output(spec: GraphSpec, state: GraphState) -> Any:
    """Resolve the spec's ``output`` binding tree against the final *state*.

    Lenient on missing refs (``missing_ok=True``): a ref into a branch a
    conditional edge skipped yields ``None`` rather than raising.
    """
    return resolve_binding(spec.output, state, missing_ok=True)
