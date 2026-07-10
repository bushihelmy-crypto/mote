"""``BgGraph`` builder — langgraph-style declarative API.

Compiles to an async ``executor(**initial_state) -> BgTaskResult`` whose ``poll``
is the frontier-scheduler driver coroutine.  See :mod:`engine` for execution.
"""

from __future__ import annotations

import typing
from typing import Any, Awaitable, Callable, Optional, Union

from metagpt.common.utils.docstring import first_line
from metagpt.executor.tasks.bggraph.base_node import (
    BaseNode,
    _parse_params_from_docstring,
)
from metagpt.executor.tasks.bggraph.channels import derive_reducers
from metagpt.executor.tasks.bggraph.engine import _build_executor
from metagpt.executor.tasks.bggraph.engine import resume as _resume
from metagpt.executor.tasks.bggraph.engine import resume_skip as _resume_skip
from metagpt.executor.tasks.bggraph.engine import resume_skip_and_from as _rsaf
from metagpt.executor.tasks.bggraph.types import (
    END,
    START,
    GraphState,
    _ConditionalEdge,
    _Edge,
    _LlmEdge,
    _NodeDef,
    _WaitingEdge,
)
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.tool_spec_adapter import annotation_to_json_schema

# ---------------------------------------------------------------------------
# Type-compatibility check for compile-time param validation
# ---------------------------------------------------------------------------


def _types_compatible(source_type: type, target_type: type) -> bool:
    """Check whether *source_type* is assignable to *target_type* (lightweight).

    Handles:
    - ``Any`` → always compatible
    - ``Optional[T]`` (Union[T, None]) → unwraps and checks T against target
    - Concrete types → ``issubclass``

    Does NOT perform full variance analysis or generic parameter checks.
    """
    origin = getattr(source_type, "__origin__", None)

    # typing.Any is compatible with anything
    if source_type is typing.Any or target_type is typing.Any:
        return True

    # Unwrap Optional[T] → check T against target
    if origin is typing.Union:
        args = [a for a in typing.get_args(source_type) if a is not type(None)]
        if not args:
            return target_type is type(None)
        # All non-None arms must be compatible
        return all(_types_compatible(a, target_type) for a in args)

    # Concrete types — issubclass (guard against non-class types)
    try:
        return issubclass(source_type, target_type)
    except TypeError:
        # source_type isn't a proper class (e.g. a generic alias) — skip
        return True


class BgGraph:
    """Declarative multi-stage background pipeline.

    Edges are *transitions*: a single edge fires its target as soon as the
    source finishes; a multi-source edge (``add_edge(["a", "b"], "c")``) is an
    AND-join.  Cycles are allowed and bounded by ``recursion_limit`` (total node
    activations).
    """

    def __init__(
        self,
        command_name: str,
        state_schema: type[GraphState] = GraphState,
        max_restarts: int = 3,
        recursion_limit: int = 100,
    ):
        """Build an empty graph.

        Args:
            recursion_limit: Safety bound on runaway cycles, counted as the
                **total number of node activations** across the whole run — not
                super-steps. This differs from LangGraph, whose ``recursion_limit``
                counts *super-steps* (BSP waves), so a wide parallel fan-out of N
                nodes is one super-step there but N activations here. The
                forward-frontier scheduler has no super-step concept (nodes are
                spawned and reaped individually as they finish), so activations
                is the only natural unit. Practical consequence for anyone porting
                a graph from LangGraph: a graph with broad fan-out or many short
                parallel branches reaches this limit far sooner than the same
                ``recursion_limit`` would in LangGraph — size it by expected total
                node runs, not by cycle depth.
        """
        self.command_name = command_name
        self.state_schema = state_schema
        self.max_restarts = max_restarts
        self.recursion_limit = recursion_limit
        self._nodes: dict[str, _NodeDef] = {}
        self._edges: list[_Edge] = []
        self._waiting_edges: list[_WaitingEdge] = []
        self._conditional_edges: list[_ConditionalEdge] = []
        self._llm_edges: list[_LlmEdge] = []
        # field name → reducer, derived from ``state_schema`` Annotated metadata
        # at compile time (see ``compile``).
        self._reducers: dict[str, Callable] = {}

    # --- node registration ---

    def node(
        self,
        name: str,
        params: Optional[dict[str, dict]] = None,
    ):
        """Decorator: register a node. ``description`` is taken from the docstring.

        Retry policy is owned by the engine (fixed budget + exponential backoff),
        so nodes expose no retry knobs.
        """

        def decorator(fn):
            self.add_node(name, fn, params=params)
            return fn

        return decorator

    def add_node(
        self,
        name: str,
        fn: Union[Callable, BaseNode, type],
        params: Optional[dict[str, dict]] = None,
    ) -> None:
        """Imperatively register a node.

        Accepts:
          - A plain async function ``(state) -> Stage``
          - A ``BaseNode`` subclass (instantiated automatically)
          - A ``BaseNode`` instance

        If ``params`` is None, auto-extracts from the function's ``Params:``
        docstring section (same convention as BaseTool's ``Args:`` for schema).
        """
        # Resolve BaseNode class → instance → bound method.
        if isinstance(fn, type) and issubclass(fn, BaseNode):
            instance = fn()
            actual_fn = instance.call
            desc = fn.get_description()
            auto_params = fn.get_params() if params is None else params
        elif isinstance(fn, BaseNode):
            actual_fn = fn.call
            desc = type(fn).get_description()
            auto_params = type(fn).get_params() if params is None else params
        else:
            actual_fn = fn
            desc = first_line(fn)
            auto_params = _parse_params_from_docstring(fn) if params is None else params

        self._nodes[name] = _NodeDef(
            name=name,
            fn=actual_fn,
            description=desc,
            params=auto_params,
        )

    # --- edge registration ---

    def add_edge(self, from_node: Union[str, list[str]], to_node: str) -> None:
        """Add an edge.

        * ``add_edge("a", "b")`` — single edge, fires on ``a`` completion.
        * ``add_edge(["a", "b"], "c")`` — waiting-edge (AND-join): ``c`` fires
          only once both ``a`` and ``b`` complete.
        """
        if isinstance(from_node, (list, tuple)):
            sources = tuple(from_node)
            if len(sources) == 1:
                self._edges.append(_Edge(sources[0], to_node))
            else:
                self._waiting_edges.append(_WaitingEdge(sources=sources, to_node=to_node))
        else:
            self._edges.append(_Edge(from_node, to_node))

    def add_conditional_edges(
        self,
        from_node: str,
        router: Callable[[GraphState], str],
        mapping: dict[str, str],
    ) -> None:
        self._conditional_edges.append(_ConditionalEdge(from_node, router, mapping))

    def add_llm_edges(self, from_node: str, prompt: str, mapping: dict[str, str]) -> None:
        """Register an LLM-in-the-loop edge.

        The graph pauses after *from_node* completes and pushes ``prompt`` +
        ``mapping`` options to the LLM, which resumes via ``resume_tasks``.
        """
        self._llm_edges.append(_LlmEdge(from_node, prompt, mapping))

    def is_self_loop(self, name: str) -> bool:
        """True if *name* has a conditional/LLM route back to itself (a ring).

        A ring node re-activates itself once per lap (e.g. ``review_batch``
        walking a file list one batch per lap), so it emits one completion
        notification per lap — many identical "node completed" messages are
        normal progress, not a stall or restart. Callers use this to label such
        completions so the repetition is not misread.
        """
        for ce in self._conditional_edges:
            if ce.from_node == name and name in ce.mapping.values():
                return True
        for le in self._llm_edges:
            if le.from_node == name and name in le.mapping.values():
                return True
        return False

    # --- compilation ---

    def compile(self) -> Callable[..., Awaitable[BgTaskResult]]:
        """Validate and compile to an async ``executor(**kwargs) -> BgTaskResult``."""
        self._normalize_waiting_edges()
        self._validate()
        self._validate_params()
        self._reducers = derive_reducers(self.state_schema)

        return _build_executor(self)

    # --- resume (delegates to engine) ---

    def resume(self, state: GraphState, from_nodes: list[str], run_state: Any = None) -> BgTaskResult:
        return _resume(self, state, from_nodes, run_state)

    def resume_skip(self, state: GraphState, skip_nodes: list[str], run_state: Any = None) -> BgTaskResult:
        return _resume_skip(self, state, skip_nodes, run_state)

    def resume_skip_and_from(
        self,
        state: GraphState,
        skip_nodes: list[str],
        from_nodes: list[str],
        run_state: Any = None,
    ) -> BgTaskResult:
        return _rsaf(self, state, skip_nodes, from_nodes, run_state)

    @property
    def stage_summary(self) -> str:
        return self._build_stage_summary()

    # --- schema introspection ---

    def get_input_schema(self) -> dict:
        """JSON Schema for the graph's initial inputs (from ``state_schema``).

        Equivalent to ``state_schema.model_json_schema()`` — a pydantic freebie.
        """
        return self.state_schema.model_json_schema()

    def get_node_schemas(self) -> dict[str, dict]:
        """Per-node JSON Schemas: ``{node_name: schema_dict}``.

        Each schema is derived from the node's declared params (type + description).
        Nodes without typed params get an empty-properties schema.
        """

        result: dict[str, dict] = {}
        for name, node_def in self._nodes.items():
            properties: dict[str, dict] = {}
            required: list[str] = []
            for pname, pinfo in node_def.params.items():
                ptype = pinfo.get("type")
                if ptype is not None:
                    prop = annotation_to_json_schema(ptype)
                else:
                    prop = {"type": "string"}
                desc = pinfo.get("desc")
                if desc:
                    prop["description"] = desc
                source = pinfo.get("from")
                if source:
                    prop["x-source"] = source
                properties[pname] = prop
                required.append(pname)
            schema: dict = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            result[name] = schema
        return result

    # --- normalization ---

    def _normalize_waiting_edges(self) -> None:
        """Fold redundant *static* trigger channels into their AND-join target.

        A waiting-edge target must fire exactly once, after *all* its sources
        arrive. If the same target is also reachable by a plain single edge, or
        by more than one waiting-edge, those are independent static channels that
        would each fire it again (a structural double-fire). The only fire-once
        reading is "wait for the union of all sources", so we merge them into a
        single waiting-edge over that union and drop the now-absorbed single
        edges. Idempotent — safe to call once per compile.

        Dynamic routes (conditional / LLM) into a join target cannot be folded
        (a sometimes-firing route has no place in an all-must-arrive join); those
        are left for ``_validate`` to reject. ``START``/``END`` are never folded.
        """
        # Group every waiting-edge by target, collecting the union of sources.
        if not any(we.to_node != END for we in self._waiting_edges):
            return
        merged: dict[str, list[str]] = {}  # target → ordered unique sources
        for we in self._waiting_edges:
            target = we.to_node
            if target == END:
                continue
            bucket = merged.setdefault(target, [])
            for s in we.sources:
                if s not in bucket:
                    bucket.append(s)

        # Absorb plain single edges that point at a join target (skip START).
        surviving_single: list[_Edge] = []
        for e in self._edges:
            if e.to_node in merged and e.from_node != START:
                if e.from_node not in merged[e.to_node]:
                    merged[e.to_node].append(e.from_node)
                continue  # folded into the join — drop the single edge
            surviving_single.append(e)
        self._edges = surviving_single

        # Rebuild waiting-edges: one merged AND-join per target. Preserve any
        # waiting-edge to END untouched (END is exempt — many nodes finish there).
        rebuilt: list[_WaitingEdge] = [we for we in self._waiting_edges if we.to_node == END]
        for target, sources in merged.items():
            rebuilt.append(_WaitingEdge(sources=tuple(sources), to_node=target))
        self._waiting_edges = rebuilt

    # --- validation ---

    def _validate(self) -> None:
        # Node names no longer become state attributes (results are merged into
        # declared state *fields*), so there is no reserved-attr collision to
        # guard against here.
        all_names = set(self._nodes.keys()) | {START, END}

        for edge in self._edges:
            if edge.from_node not in all_names:
                raise ValueError(f"Unknown node: {edge.from_node}")
            if edge.to_node not in all_names:
                raise ValueError(f"Unknown node: {edge.to_node}")
        for we in self._waiting_edges:
            for s in we.sources:
                if s not in all_names:
                    raise ValueError(f"Unknown source in waiting edge: {s}")
            if we.to_node not in all_names:
                raise ValueError(f"Unknown target in waiting edge: {we.to_node}")
        for ce in self._conditional_edges:
            if ce.from_node not in all_names:
                raise ValueError(f"Unknown node in conditional edge: {ce.from_node}")
            for target in ce.mapping.values():
                if target not in all_names:
                    raise ValueError(f"Unknown target in conditional edge: {target}")
        for le in self._llm_edges:
            if le.from_node not in all_names:
                raise ValueError(f"Unknown node in LLM edge: {le.from_node}")
            for target in le.mapping.values():
                if target not in all_names:
                    raise ValueError(f"Unknown target in LLM edge: {target}")

        start_edges = [e for e in self._edges if e.from_node == START]
        if not start_edges:
            raise ValueError("Graph must have at least one edge from START")

        has_end = (
            any(e.to_node == END for e in self._edges)
            or any(we.to_node == END for we in self._waiting_edges)
            or any(END in ce.mapping.values() for ce in self._conditional_edges)
            or any(END in le.mapping.values() for le in self._llm_edges)
        )
        if not has_end:
            raise ValueError("Graph must have at least one edge to END")

        # A waiting-edge (AND-join) target fires exactly once, when all of its
        # sources arrive. ``_normalize_waiting_edges`` (run before validation in
        # ``compile``) already folded any *static* single/waiting channels into
        # that join, so the only remaining conflict here is a *dynamic* route
        # (conditional / LLM) into a join target. A sometimes-firing route has no
        # place in an all-must-arrive join — it cannot be merged — so it is the
        # one case still rejected.
        for we in self._waiting_edges:
            target = we.to_node
            if target == END:
                continue
            dynamic_sources: list[str] = []
            for ce in self._conditional_edges:
                if target in ce.mapping.values():
                    dynamic_sources.append(f"router({ce.from_node})")
            for le in self._llm_edges:
                if target in le.mapping.values():
                    dynamic_sources.append(f"llm({le.from_node})")
            if dynamic_sources:
                join = "+".join(we.sources)
                raise ValueError(
                    f"Node '{target}' is an AND-join target (waiting-edge "
                    f"[{join}]) but is also a dynamic route target from "
                    f"{dynamic_sources}. A conditional/LLM route fires "
                    f"conditionally and cannot be folded into an all-sources "
                    f"join, so it would fire the target a second time. Route "
                    f"the dynamic edge to a distinct node, or to one of the "
                    f"join's sources."
                )

        # NOTE: cycles are intentionally allowed (langgraph model). They are
        # bounded at runtime by ``recursion_limit``; no compile-time check.

    def _validate_params(self) -> None:
        for name, node_def in self._nodes.items():
            for param_name, param_info in node_def.params.items():
                source = param_info["from"]
                if source.startswith("$input."):
                    field_name = source[len("$input.") :]
                    if field_name not in self.state_schema.model_fields:
                        raise ValueError(
                            f"Node '{name}' param '{param_name}' references " f"unknown input field: {field_name}"
                        )
                    # Type compatibility check for $input fields
                    expected_type = param_info.get("type")
                    if expected_type is not None:
                        field_info = self.state_schema.model_fields[field_name]
                        if field_info.annotation is not None:
                            source_type = field_info.annotation
                            if not _types_compatible(source_type, expected_type):
                                raise ValueError(
                                    f"Node '{name}' param '{param_name}': "
                                    f"input field '{field_name}' is {source_type}, "
                                    f"expected {expected_type}"
                                )
                else:
                    # Non-$input source references a state *field* (the first
                    # dotted segment). With the field/channel model any node may
                    # write any field — and ``extra="allow"`` lets undeclared
                    # fields land at runtime — so an undeclared reference is not
                    # a compile-time error. Declared fields are accepted as-is;
                    # nothing to reject here.
                    pass

    # --- helpers ---

    def _build_predecessors(self) -> dict[str, set[str]]:
        """Static predecessors (single + waiting edges). Conditional/LLM dynamic."""
        preds: dict[str, set[str]] = {name: set() for name in self._nodes}
        for edge in self._edges:
            if edge.to_node in preds and edge.from_node != START:
                preds[edge.to_node].add(edge.from_node)
        for we in self._waiting_edges:
            if we.to_node in preds:
                for s in we.sources:
                    if s != START:
                        preds[we.to_node].add(s)
        return preds

    def _get_entry_nodes(self) -> list[str]:
        """All START targets — multiple entry nodes fan out in parallel.

        Mirrors langgraph: ``add_edge(START, x)`` for several ``x`` seeds them as
        concurrent entry points. Deduped, preserving declaration order.
        """
        return list(dict.fromkeys(e.to_node for e in self._edges if e.from_node == START))

    def _get_finish_nodes(self) -> list[str]:
        finish = [e.from_node for e in self._edges if e.to_node == END]
        for we in self._waiting_edges:
            if we.to_node == END:
                finish.extend(we.sources)
        for ce in self._conditional_edges:
            if END in ce.mapping.values():
                finish.append(ce.from_node)
        for le in self._llm_edges:
            if END in le.mapping.values():
                finish.append(le.from_node)
        return list(dict.fromkeys(finish))

    def _build_stage_summary(self) -> str:
        """Best-effort layered summary. Cyclic remainder is appended by name.

        Annotates waiting-edges (``→ merge [join: tts & render]``), conditional
        routes (``─router→``) and LLM routes (``─LLM route→``).
        """
        predecessors = self._build_predecessors()
        in_degree = {name: len(preds) for name, preds in predecessors.items()}
        order: list[str] = []
        remaining = set(self._nodes.keys())
        while remaining:
            layer = sorted(n for n in remaining if in_degree.get(n, 0) <= 0)
            if not layer:
                break
            order.extend(layer)
            remaining -= set(layer)
            for n in layer:
                for edge in self._edges:
                    if edge.from_node == n and edge.to_node in in_degree:
                        in_degree[edge.to_node] -= 1
                for we in self._waiting_edges:
                    if n in we.sources and we.to_node in in_degree:
                        in_degree[we.to_node] -= 1
        # cyclic / unreached remainder, appended deterministically by name
        order.extend(sorted(remaining))

        lines: list[str] = []
        for name in order:
            node_def = self._nodes[name]
            desc = node_def.description
            prefix = f"{name}({desc})" if desc else name
            parts: list[str] = []

            regular = [e.to_node for e in self._edges if e.from_node == name]
            if regular:
                parts.append("→ " + ", ".join(regular))

            for we in self._waiting_edges:
                if name in we.sources:
                    join = " & ".join(we.sources)
                    parts.append(f"→ {we.to_node} [join: {join}]")

            for ce in self._conditional_edges:
                if ce.from_node == name:
                    routes = " | ".join(f"{k}: {v}" for k, v in ce.mapping.items())
                    parts.append(f"─router→ {routes}")

            for le in self._llm_edges:
                if le.from_node == name:
                    routes = " | ".join(k if v == END else f"{k}: {v}" for k, v in le.mapping.items())
                    parts.append(f"─LLM route→ {routes}")

            if not parts:
                parts.append("→ END")
            lines.append(f"  {prefix} {'; '.join(parts)}")

        return "\n".join(lines)
