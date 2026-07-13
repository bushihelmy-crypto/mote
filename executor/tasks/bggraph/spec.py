"""Declarative graph spec — the model-facing contract for the ``run_graph`` tool.

The LLM authors a :class:`GraphSpec` describing tool orchestration as *data*
(nodes + edges) rather than imperative code. The compiler (``from_spec.py``)
turns a validated spec into a runnable :class:`BgGraph`; nothing in this module
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
* ``{"$ref": "node"}`` / ``{"$ref": "node.key"}`` — another node's result, or a
  sub-key of it. Inside a ``map`` node it may also name the ``as`` loop variable.
* anything else is a **literal**. Literals nest: a dict's values and a list's
  items are each themselves bindings, so ``{"opts": [{"$ref": "a"}, 2]}`` mixes a
  reference and a constant.

Edges are usually *derived automatically* from the ``$ref``s a node consumes; the
explicit ``edges`` list is only needed to add ordering the data flow doesn't imply
or to branch via a ``when`` predicate.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import END, START

# ---------------------------------------------------------------------------
# Binding grammar
# ---------------------------------------------------------------------------

INPUT_KEY = "$input"
REF_KEY = "$ref"
_MAGIC_KEYS = (INPUT_KEY, REF_KEY)

NodeKind = Literal["tool", "map", "compute"]

# Comparison operators for a conditional edge's ``when`` predicate. Unary ops
# ignore ``right``; every other op compares ``left`` against ``right``.
CompareOp = Literal["eq", "ne", "gt", "lt", "ge", "le", "in", "not_in", "contains", "truthy", "falsy"]
UNARY_OPS = frozenset({"truthy", "falsy"})

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
            if len(value) != 1:
                raise ValueError(
                    f"{where}: a binding using {present[0]!r} must have exactly that one key, " f"got {sorted(value)}"
                )
            payload = value[present[0]]
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError(f"{where}: {present[0]!r} must be a non-empty string")
            return
        for key, item in value.items():
            _validate_binding(item, where=f"{where}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_binding(item, where=f"{where}[{idx}]")
    # scalars (str/int/float/bool/None) are literals — always valid.


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
      Its result is the list of per-item results.
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

    # map only
    over: Any = Field(default=None, description="map: binding to the collection to iterate.")
    as_: Optional[str] = Field(
        default=None,
        alias="as",
        description='map: loop-variable name; reference the current item via {"$ref": "<as>"}.',
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

        # ``concurrency`` has a non-None default (8), so the None-sentinel
        # ``_forbid`` can't police it — instead reject it on non-map kinds only
        # when the caller set it *explicitly* (present in the input).
        if self.kind != "map" and "concurrency" in self.model_fields_set:
            raise ValueError(f"node {self.id!r}: {self.kind} node must not set 'concurrency'")

        if self.kind == "tool":
            if not self.tool:
                raise ValueError(f"node {self.id!r}: tool node requires 'tool'")
            self._forbid("tool", over=self.over, expr=self.expr, **{"as": self.as_})
        elif self.kind == "map":
            if not self.tool:
                raise ValueError(f"node {self.id!r}: map node requires 'tool' (the per-item body)")
            if self.over is None:
                raise ValueError(f"node {self.id!r}: map node requires 'over' (the collection)")
            if not _is_identifier(self.as_):
                raise ValueError(f"node {self.id!r}: map node requires 'as' as a valid identifier")
            self._forbid("map", expr=self.expr)
            _validate_binding(self.over, where=f"node[{self.id}].over")
        elif self.kind == "compute":
            if not (self.expr and self.expr.strip()):
                raise ValueError(f"node {self.id!r}: compute node requires a non-empty 'expr'")
            self._forbid("compute", tool=self.tool, over=self.over, **{"as": self.as_})

        for key, value in self.args.items():
            _validate_binding(value, where=f"node[{self.id}].args.{key}")
        return self

    def _forbid(self, kind: str, **fields: Any) -> None:
        for name, value in fields.items():
            if value is not None:
                raise ValueError(f"node {self.id!r}: {kind} node must not set {name!r}")


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


class GraphSpec(BaseModel):
    """The whole orchestration the model submits to ``run_graph``.

    ``inputs`` declares the values supplied at call time; ``nodes`` are the units
    of work; ``edges`` add ordering/branching beyond what ``$ref`` data-flow
    implies; ``output`` is a binding tree resolved against the final state to
    become the tool's return value.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, InputField] = Field(default_factory=dict)
    nodes: list[NodeSpec] = Field(min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)
    output: Any = Field(description="Binding tree (dict/list/scalar with $ref/$input leaves).")

    @model_validator(mode="after")
    def _check(self) -> "GraphSpec":
        ids: set[str] = set()
        for node in self.nodes:
            if node.id in ids:
                raise ValueError(f"duplicate node id {node.id!r}")
            ids.add(node.id)

        endpoints = ids | _RESERVED_IDS
        for edge in self.edges:
            if edge.from_ not in endpoints:
                raise ValueError(f"edge 'from' {edge.from_!r} is not a declared node")
            if edge.to not in endpoints:
                raise ValueError(f"edge 'to' {edge.to!r} is not a declared node")

        _validate_binding(self.output, where="output")
        return self
