"""Declarative graph spec — the model-facing contract for the ``run_graph`` tool.

The LLM authors a :class:`GraphSpec` describing tool orchestration as *data*
(nodes + edges) rather than imperative code. The compiler (``from_spec.py``)
turns a validated spec into a runnable :class:`WorkflowBuilder`; nothing in this module
executes anything. This file is deliberately dependency-light (pydantic + stdlib)
so it can double as the JSON-schema source the model reads.

Design goals: model-agnostic, self-documenting via JSON schema, and stable — the
spec is the public interface the model must keep working against for years, so it
is kept small and orthogonal (three node kinds, one edge shape, one binding
grammar) rather than growing special cases.

Binding grammar
---------------
Any value inside ``args`` / ``over`` / ``output`` / a predicate operand is a
*binding*, resolved at run time:

* ``{"$input": "field"}``          — a graph input value.
* ``{"$ref": "node"}`` / ``{"$ref": "node.key"}`` — another node's result (or a
  sub-key of it), **or a channel's current value**. Inside a ``map`` node it may
  also name the ``as`` loop variable.
* ``{"$fmt": "template", ...}``    — a **string template**: the ``$fmt`` value is
  a Python ``str.format`` template, and every *other* key is itself a binding
  filling one ``{name}`` placeholder. It resolves to the formatted string, e.g.
  ``{"$fmt": "process {f}", "f": {"$ref": "item"}}`` → ``"process foo.txt"``. This
  is the tool-agnostic way to splice a value into a string arg (a shell command, a
  path, a URL) inline, instead of adding a ``compute`` node just to concatenate.
  It is a graph-level binding — placeholders are ``{name}``, unrelated to any
  shell ``$var``.
* anything else is a **literal**. Literals nest: a dict's values and a list's
  items are each themselves bindings, so ``{"opts": [{"$ref": "a"}, 2]}`` mixes a
  reference and a constant.

A ``$ref`` resolves against two namespaces with different edge semantics:

* a **node result** — single-assignment; consuming it adds an automatic data-flow
  edge so the consumer runs after the producer.
* a **channel** (see :class:`ChannelSpec`) — a mutable loop-carried cell with an
  initial value and a reducer; consuming it adds **no** edge, so a loop body can
  read state seeded by a previous lap without forcing a spurious ordering.

Edges are usually *derived automatically* from the node-result ``$ref``s a node
consumes; the explicit ``edges`` list is needed to add ordering the data flow
doesn't imply, to branch via a ``when`` predicate, or to form a **loop** by
pointing an edge back to an earlier node.

Execution follows the langgraph forward-frontier model, **not** a static DAG:
edges may point backward, so cycles are allowed and bounded at run time by
``recursion_limit`` (total node activations). A ``while`` loop is a guarded edge
that points back to the loop head while its condition holds, plus an ``__end__``
(or fall-through) edge for the exit.
"""

from __future__ import annotations

import math
import string
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mote.orchestration.workflows.types import END, START

# ---------------------------------------------------------------------------
# Binding grammar
# ---------------------------------------------------------------------------

INPUT_KEY = "$input"
REF_KEY = "$ref"
FMT_KEY = "$fmt"
# ``$input``/``$ref`` are *exclusive* magic keys — a binding using one carries
# only that key. ``$fmt`` is different: it names a template and the sibling keys
# are the bindings that fill it, so it is validated separately (see below).
_MAGIC_KEYS = (INPUT_KEY, REF_KEY)

NodeKind = Literal["tool", "map", "fold", "compute"]

# Comparison operators for a conditional edge's ``when`` predicate. Unary ops
# ignore ``right``; every other op compares ``left`` against ``right``.
CompareOp = Literal["eq", "ne", "gt", "lt", "ge", "le", "in", "not_in", "contains", "truthy", "falsy"]
UNARY_OPS = frozenset({"truthy", "falsy"})

# Reducer ops for a channel — how repeated / parallel writes to the same channel
# combine. ``last`` (default) is last-value (most recent write wins); the rest
# fold: ``append`` grows a list by one item, ``extend`` concatenates a list,
# ``add`` sums numbers (or concatenates strings/lists), ``or``/``and`` are
# boolean folds, ``min``/``max`` keep the extreme, ``merge`` shallow-merges
# dicts. The set is a fixed, named vocabulary (never arbitrary code) so a
# channel's merge semantics stay declarative and stable.
ReduceOp = Literal["last", "append", "extend", "add", "or", "and", "min", "max", "merge"]

# Reserved node names — the graph frontier sentinels; a user node may not reuse them.
_RESERVED_IDS = frozenset({START, END})


def as_input_ref(value: Any) -> Optional[str]:
    """Return the field name if *value* is ``{"$input": name}``, else ``None``."""
    if isinstance(value, dict) and len(value) == 1 and INPUT_KEY in value:
        payload = value[INPUT_KEY]
        return payload if isinstance(payload, str) else None
    return None


def as_node_ref(value: Any) -> Optional[str]:
    """Return the ref path if *value* is ``{"$ref": path}``, else ``None``."""
    if isinstance(value, dict) and len(value) == 1 and REF_KEY in value:
        payload = value[REF_KEY]
        return payload if isinstance(payload, str) else None
    return None


def as_fmt_template(value: Any) -> Optional[str]:
    """Return the template if *value* is a ``{"$fmt": template, ...}`` binding.

    Unlike ``$input``/``$ref``, a ``$fmt`` binding is *not* single-key: it carries
    the template under ``$fmt`` plus one sibling key per ``{name}`` placeholder,
    each a binding. Returns the template string when the value is such a dict, else
    ``None``.
    """
    if isinstance(value, dict) and FMT_KEY in value:
        template = value[FMT_KEY]
        return template if isinstance(template, str) else None
    return None


def _fmt_field_names(template: str) -> list[str]:
    """The ``{name}`` placeholder field names used by a ``str.format`` template.

    Only simple named fields are supported (no positional ``{}`` / ``{0}``); an
    attribute/index suffix (``{a.b}`` / ``{a[0]}``) uses just its head name ``a``.
    """
    names: list[str] = []
    for _literal, field_name, _spec, _conv in string.Formatter().parse(template):
        if field_name is None:
            continue
        head = field_name.replace("[", ".").split(".")[0]
        if head:
            names.append(head)
    return names


def split_ref(path: str) -> tuple[str, Optional[str]]:
    """Split a ``$ref`` path ``"node.key"`` into ``("node", "key")`` (key may be None)."""
    head, sep, tail = path.partition(".")
    return head, (tail if sep else None)


def _validate_binding(value: Any, *, where: str) -> None:
    """Recursively validate the shape of any magic-key dicts inside a binding.

    Literals and nested literals are fine; a dict carrying ``$input``/``$ref``
    must carry *only* that key and a non-empty string payload. Ref *targets*
    (does the node exist?) are checked later by the compiler, which knows the
    full node set — this only enforces well-formedness.
    """
    if isinstance(value, dict):
        present = [k for k in _MAGIC_KEYS if k in value]
        if present:
            if FMT_KEY in value:
                raise ValueError(f"{where}: a binding cannot mix {present[0]!r} with {FMT_KEY!r}")
            if len(value) != 1:
                raise ValueError(
                    f"{where}: a binding using {present[0]!r} must have exactly that one key, " f"got {sorted(value)}"
                )
            payload = value[present[0]]
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError(f"{where}: {present[0]!r} must be a non-empty string")
            return
        if FMT_KEY in value:
            template = value[FMT_KEY]
            if not isinstance(template, str) or not template.strip():
                raise ValueError(f"{where}: {FMT_KEY!r} must be a non-empty template string")
            # Every ``{name}`` placeholder must have a sibling key providing it, and
            # every sibling (a fill value) is itself a binding — recurse into them.
            fields = set(_fmt_field_names(template))
            siblings = {k for k in value if k != FMT_KEY}
            missing = fields - siblings
            if missing:
                raise ValueError(
                    f"{where}: {FMT_KEY!r} template references {sorted(missing)} with no matching sibling binding"
                )
            for key, item in value.items():
                if key == FMT_KEY:
                    continue
                _validate_binding(item, where=f"{where}.{key}")
            return
        for key, item in value.items():
            _validate_binding(item, where=f"{where}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_binding(item, where=f"{where}[{idx}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: non-finite numeric literals are forbidden")
    # Remaining scalars (str/int/finite float/bool/None) are literals.


def _is_identifier(name: Any) -> bool:
    return isinstance(name, str) and name.isidentifier()


# ---------------------------------------------------------------------------
# Spec models
# ---------------------------------------------------------------------------


class InputField(BaseModel):
    """Declares one graph input so ``{"$input": name}`` references can be checked."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "integer", "boolean", "list", "object", "any"] = "any"
    description: str = ""
    required: bool = True


class ChannelSpec(BaseModel):
    """Declares a mutable, cross-node *channel* — the loop-carried state cell.

    A ``$ref`` names one of two things: a **node result** (single-assignment,
    produced once, and its consumers get an auto data-flow edge so they run
    after it) or a **channel** (this). A channel is different in three ways that
    make it exactly what a loop body needs:

    * it has an ``initial`` value, so a node may read it on the very first lap
      before anything has written it (a node-result ref before its producer runs
      is a wiring error);
    * repeated / parallel writes fold through ``reduce`` (e.g. ``extend`` to
      accumulate a growing list, ``add`` to keep a running total) rather than
      just overwriting;
    * a ``$ref`` to a channel does **not** create a data-flow edge, so reading a
      channel never forces ordering — ordering around a loop is expressed by the
      explicit (possibly back-pointing) edges instead.

    A node writes a channel by setting its ``writes`` to the channel name; the
    node's produced value is then merged into that channel through ``reduce``
    (instead of being stored under the node's own id).

    ``initial`` is a plain literal (not a binding) — it is baked into the state
    at graph construction time, before any input is resolved.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "integer", "boolean", "list", "object", "any"] = "any"
    description: str = ""
    initial: Any = Field(
        default=None,
        description="Literal starting value, readable before the first write (e.g. [] for an accumulator, 0 for a counter).",
    )
    reduce: ReduceOp = Field(
        default="last",
        description="How repeated/parallel writes combine: last (overwrite) | append | extend | add | or | and | min | max | merge.",
    )


class Predicate(BaseModel):
    """A boolean test used as an edge guard (``when``).

    ``left``/``right`` are bindings (``$ref`` into state, or literals). Unary ops
    (``truthy``/``falsy``) test ``left`` alone and forbid ``right``; all other ops
    compare ``left`` against ``right``. To test for null use ``falsy`` rather than
    ``eq`` against a null literal.
    """

    model_config = ConfigDict(extra="forbid")

    left: Any = Field(description="Binding to test — typically a $ref into node/input state.")
    op: CompareOp
    right: Any = Field(
        default=None,
        description="Comparison operand (binding or literal). Omit for unary ops (truthy/falsy).",
    )

    @model_validator(mode="after")
    def _check(self) -> "Predicate":
        if self.op in UNARY_OPS:
            if self.right is not None:
                raise ValueError(f"predicate op {self.op!r} is unary and must not carry 'right'")
        else:
            if self.right is None:
                raise ValueError(
                    f"predicate op {self.op!r} needs a 'right' operand " f"(use 'falsy' to test for null/empty)"
                )
        _validate_binding(self.left, where="predicate.left")
        if self.right is not None:
            _validate_binding(self.right, where="predicate.right")
        return self


class NodeSpec(BaseModel):
    """One graph node. ``kind`` selects which extra fields are meaningful.

    * ``tool``    — call a tool: needs ``tool`` + ``args``.
    * ``map``     — fan out a tool over a collection in parallel: needs ``over``
      (the collection binding), ``as`` (the per-item loop variable name), and the
      tool body (``tool`` + ``args``, where ``{"$ref": "<as>"}`` is the item).
      Its result is the list of per-item results. ``on_item_error`` isolates a
      failed item (``skip``, the map default — batch continues) or sinks the whole
      node (``fail``).
    * ``fold``    — map's *serial* twin: run a tool over a collection one item at
      a time, threading an accumulator so each step sees the state built by the
      previous ones. Needs ``over`` + ``as`` (as map), plus ``acc`` (the
      accumulator variable name, read in the body via ``{"$ref": "<acc>"}``),
      ``initial`` (its starting value), and ``reduce`` (how each item's tool
      result folds into the accumulator — the same vocabulary as a channel). Its
      result is the final accumulator. Use it when items depend on each other
      (later steps read earlier results); use ``map`` when they are independent.
      ``on_item_error`` defaults to ``fail`` here (a skipped item would break the
      chain the later ones read).
    * ``compute`` — evaluate an ``expr`` (a restricted Python expression, or a
      short statement block ending in an expression) with ``args`` bound as local
      names. A curated set of pure stdlib modules is in scope (re, json, math,
      statistics, itertools, functools, collections, textwrap, string, datetime,
      base64, hashlib) for parsing / filtering / reshaping data. No tool call, no
      import, no file / network / side effects.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(description="Unique node id; a valid identifier, referenced by $ref.")
    kind: NodeKind
    description: str = ""

    # tool / map body
    tool: Optional[str] = Field(default=None, description="Tool name (tool & map kinds).")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments; each value is a binding (literal / $input / $ref).",
    )

    # Any kind: redirect this node's produced value into a declared channel
    # (merged through the channel's reducer) instead of the default node-result
    # slot. A node with ``writes`` produces no ``$ref``-able own-id result — its
    # output lives in the channel, so downstream reads ``{"$ref": "<channel>"}``.
    writes: Optional[str] = Field(
        default=None,
        description="Channel name to write this node's result into (via its reducer). Omit to store under the node id.",
    )

    # map + fold: iterate a tool body over a collection.
    over: Any = Field(default=None, description="map/fold: binding to the collection to iterate.")
    as_: Optional[str] = Field(
        default=None,
        alias="as",
        description='map/fold: per-item loop-variable name; reference the current item via {"$ref": "<as>"}.',
    )
    concurrency: int = Field(
        default=8,
        gt=0,
        description=(
            "map: max items evaluated at once (a positive int, default 8). Bounds "
            "fan-out so a large collection does not launch every item's tool call "
            "simultaneously (the concurrency cap). Raise it for cheap/independent "
            "items, lower it for heavy ones."
        ),
    )
    on_item_error: Optional[Literal["fail", "skip"]] = Field(
        default=None,
        description=(
            "map/fold: on one item's permanent failure, 'skip' it (drops from the "
            "result / not folded, batch continues, skips reported back) or 'fail' "
            "the whole node. Default fits the kind: map='skip' (independent items), "
            "fold='fail' (dependent — a gap breaks the chain). Set only to override. "
            "Even under 'skip', if EVERY item fails the node still fails (systematic "
            "error, not isolated)."
        ),
    )

    # fold only: the accumulator threaded through the serial iteration.
    acc: Optional[str] = Field(
        default=None,
        description='fold: accumulator variable name; read the running value in the body via {"$ref": "<acc>"}.',
    )
    initial: Any = Field(
        default=None,
        description="fold: literal starting value of the accumulator (e.g. {} to build a dict, [] a list, 0 a total).",
    )
    reduce: ReduceOp = Field(
        default="last",
        description=(
            "fold: how each item's tool result folds into the accumulator — "
            "last (replace) | append | extend | add | or | and | min | max | merge. "
            "Same vocabulary as a channel."
        ),
    )

    # compute only
    expr: Optional[str] = Field(
        default=None,
        description=(
            "compute: a restricted Python expression (or short statement block ending in an "
            "expression) evaluated over args. Pure stdlib modules are in scope (re, json, math, "
            "statistics, itertools, functools, collections, textwrap, string, datetime, base64, "
            "hashlib); no import, no file/network/tool access."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "NodeSpec":
        if not _is_identifier(self.id):
            raise ValueError(f"node id {self.id!r} must be a valid identifier")
        if self.id in _RESERVED_IDS:
            raise ValueError(f"node id {self.id!r} is reserved")

        # Fields with a non-None default (``concurrency``=8, ``reduce``="last")
        # cannot be policed by the None-sentinel ``_forbid`` — instead reject them
        # on the wrong kinds only when the caller set them *explicitly* (present
        # in the input). ``concurrency`` is map-only; ``reduce`` is fold-only.
        if self.kind != "map" and "concurrency" in self.model_fields_set:
            raise ValueError(f"node {self.id!r}: {self.kind} node must not set 'concurrency'")
        if self.kind != "fold" and "reduce" in self.model_fields_set:
            raise ValueError(f"node {self.id!r}: {self.kind} node must not set 'reduce'")
        if self.kind != "fold" and "initial" in self.model_fields_set:
            raise ValueError(f"node {self.id!r}: {self.kind} node must not set 'initial'")

        if self.kind == "tool":
            if not self.tool:
                raise ValueError(f"node {self.id!r}: tool node requires 'tool'")
            self._forbid(
                "tool",
                over=self.over,
                expr=self.expr,
                acc=self.acc,
                on_item_error=self.on_item_error,
                **{"as": self.as_},
            )
        elif self.kind == "map":
            if not self.tool:
                raise ValueError(f"node {self.id!r}: map node requires 'tool' (the per-item body)")
            if self.over is None:
                raise ValueError(f"node {self.id!r}: map node requires 'over' (the collection)")
            if not _is_identifier(self.as_):
                raise ValueError(f"node {self.id!r}: map node requires 'as' as a valid identifier")
            self._forbid("map", expr=self.expr, acc=self.acc)
            _validate_binding(self.over, where=f"node[{self.id}].over")
        elif self.kind == "fold":
            if not self.tool:
                raise ValueError(f"node {self.id!r}: fold node requires 'tool' (the per-item body)")
            if self.over is None:
                raise ValueError(f"node {self.id!r}: fold node requires 'over' (the collection)")
            if not _is_identifier(self.as_):
                raise ValueError(f"node {self.id!r}: fold node requires 'as' as a valid identifier")
            if not _is_identifier(self.acc):
                raise ValueError(f"node {self.id!r}: fold node requires 'acc' as a valid identifier")
            if self.as_ == self.acc:
                raise ValueError(f"node {self.id!r}: fold 'as' and 'acc' must be different names")
            self._forbid("fold", expr=self.expr)
            _validate_binding(self.over, where=f"node[{self.id}].over")
        elif self.kind == "compute":
            if not (self.expr and self.expr.strip()):
                raise ValueError(f"node {self.id!r}: compute node requires a non-empty 'expr'")
            self._forbid(
                "compute",
                tool=self.tool,
                over=self.over,
                acc=self.acc,
                on_item_error=self.on_item_error,
                **{"as": self.as_},
            )

        if self.writes is not None and not _is_identifier(self.writes):
            raise ValueError(f"node {self.id!r}: 'writes' must be a valid channel identifier, got {self.writes!r}")

        for key, value in self.args.items():
            _validate_binding(value, where=f"node[{self.id}].args.{key}")
        return self

    def _forbid(self, kind: str, **fields: Any) -> None:
        for name, value in fields.items():
            if value is not None:
                raise ValueError(f"node {self.id!r}: {kind} node must not set {name!r}")

    @property
    def effective_on_item_error(self) -> Literal["fail", "skip"]:
        """The per-item failure policy, resolving the kind-specific default.

        ``on_item_error`` defaults to ``None`` (unset) so the effective policy can
        match each kind's nature: ``map`` items are independent → ``skip`` (one
        failure must not discard the successful rest); ``fold`` items are dependent
        (each reads the accumulator built by prior ones) → ``fail`` (a skipped item
        would break the chain). An explicit value always wins.
        """
        if self.on_item_error is not None:
            return self.on_item_error
        return "skip" if self.kind == "map" else "fail"


class EdgeSpec(BaseModel):
    """An explicit edge ``from → to``, optionally guarded by a ``when`` predicate.

    Unguarded edges fire as soon as ``from`` completes. Several guarded edges out
    of the same node form an if/elif chain (first matching predicate wins); add a
    final unguarded edge as the ``else`` fallthrough. ``from``/``to`` are node ids
    or the ``__start__`` / ``__end__`` sentinels.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: str = Field(alias="from")
    to: str
    when: Optional[Predicate] = None


class GraphOutputContractSpec(BaseModel):
    """Stable typed terminal contract for a model-authored graph."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = "mote"
    name: str = "graph-json"
    version: str = "1"
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    @model_validator(mode="after")
    def _identity_required(self) -> "GraphOutputContractSpec":
        if not self.namespace.strip() or not self.name.strip() or not self.version.strip():
            raise ValueError("output contract namespace, name and version are required")
        return self


class GraphSpec(BaseModel):
    """The whole orchestration the model submits to ``run_graph``.

    ``inputs`` declares the values supplied at call time; ``channels`` declare
    mutable loop-carried state cells; ``nodes`` are the units of work; ``edges``
    add ordering/branching (and back-edges for loops) beyond what ``$ref``
    data-flow implies; ``output`` is a binding tree resolved against the final
    state to become the tool's return value.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, InputField] = Field(default_factory=dict)
    channels: dict[str, ChannelSpec] = Field(
        default_factory=dict,
        description="Mutable loop-carried state cells (initial value + reducer); read/written by $ref, never creating a data-flow edge.",
    )
    nodes: list[NodeSpec] = Field(min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)
    output: Any = Field(description="Binding tree (dict/list/scalar with $ref/$input leaves).")
    output_contract: GraphOutputContractSpec = Field(
        default_factory=GraphOutputContractSpec,
        description="Typed terminal contract; defaults to any JSON value.",
    )
    recursion_limit: Optional[int] = Field(
        default=None,
        gt=0,
        le=10000,
        description="Total node-activation bound guarding runaway loops (a positive int; default 100 when omitted, hard cap 10000).",
    )

    @model_validator(mode="after")
    def _check(self) -> "GraphSpec":
        ids: set[str] = set()
        for node in self.nodes:
            if node.id in ids:
                raise ValueError(f"duplicate node id {node.id!r}")
            ids.add(node.id)

        # Three ref namespaces must stay disjoint: node ids, input names, and
        # channel names. A ``$ref`` head is resolved against nodes+channels and
        # ``$input`` against inputs; a name living in two namespaces would make a
        # ref ambiguous, so it is rejected up front.
        input_names = set(self.inputs)
        channel_names = set(self.channels)
        for a_name, a_set, b_name, b_set in (
            ("node id", ids, "input", input_names),
            ("node id", ids, "channel", channel_names),
            ("input", input_names, "channel", channel_names),
        ):
            clash = a_set & b_set
            if clash:
                raise ValueError(f"{a_name}(s) collide with {b_name} name(s): {sorted(clash)}")

        # A channel name must be a valid identifier (it becomes a state field and
        # a ``$ref`` head).
        for cname, channel in self.channels.items():
            if not _is_identifier(cname):
                raise ValueError(f"channel name {cname!r} must be a valid identifier")
            _validate_binding(channel.initial, where=f"channel[{cname}].initial")

        # Every ``writes`` must target a declared channel.
        for node in self.nodes:
            if node.writes is not None and node.writes not in channel_names:
                raise ValueError(f"node {node.id!r}: 'writes' target {node.writes!r} is not a declared channel")

        endpoints = ids | _RESERVED_IDS
        for edge in self.edges:
            if edge.from_ not in endpoints:
                raise ValueError(f"edge 'from' {edge.from_!r} is not a declared node")
            if edge.to not in endpoints:
                raise ValueError(f"edge 'to' {edge.to!r} is not a declared node")

        _validate_binding(self.output, where="output")
        return self
