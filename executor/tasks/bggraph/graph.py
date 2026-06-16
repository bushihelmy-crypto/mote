"""``BgGraph`` builder — langgraph-style declarative API.

Compiles to an async ``executor(**initial_state) -> BgTaskResult`` whose ``poll``
is the frontier-scheduler driver coroutine.  See :mod:`engine` for execution.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Union

from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.tasks.bggraph.types import (
    END,
    START,
    GraphState,
    BgStatus,
    _ConditionalEdge,
    _Edge,
    _LlmEdge,
    _NodeDef,
    _WaitingEdge,
)


def _docstring_first_line(fn: Callable) -> str:
    return (fn.__doc__ or "").strip().split("\n")[0]


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
        self.command_name = command_name
        self.state_schema = state_schema
        self.max_restarts = max_restarts
        self.recursion_limit = recursion_limit
        self._nodes: dict[str, _NodeDef] = {}
        self._edges: list[_Edge] = []
        self._waiting_edges: list[_WaitingEdge] = []
        self._conditional_edges: list[_ConditionalEdge] = []
        self._llm_edges: list[_LlmEdge] = []

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
        fn: Callable,
        params: Optional[dict[str, dict]] = None,
    ) -> None:
        """Imperatively register a node."""
        self._nodes[name] = _NodeDef(
            name=name,
            fn=fn,
            description=_docstring_first_line(fn),
            params=params or {},
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

    # --- compilation ---

    def compile(self) -> Callable[..., Awaitable[BgTaskResult]]:
        """Validate and compile to an async ``executor(**kwargs) -> BgTaskResult``."""
        self._validate()
        self._validate_params()
        from metagpt.executor.tasks.bggraph.engine import _build_executor

        return _build_executor(self)

    # --- resume (delegates to engine) ---

    def resume(self, state: GraphState, from_nodes: list[str]) -> BgTaskResult:
        from metagpt.executor.tasks.bggraph.engine import resume as _resume

        return _resume(self, state, from_nodes)

    def resume_skip(self, state: GraphState, skip_nodes: list[str]) -> BgTaskResult:
        from metagpt.executor.tasks.bggraph.engine import resume_skip as _resume_skip

        return _resume_skip(self, state, skip_nodes)

    def resume_skip_and_from(
        self, state: GraphState, skip_nodes: list[str], from_nodes: list[str]
    ) -> BgTaskResult:
        from metagpt.executor.tasks.bggraph.engine import resume_skip_and_from as _rsaf

        return _rsaf(self, state, skip_nodes, from_nodes)

    @property
    def stage_summary(self) -> str:
        return self._build_stage_summary()

    # --- validation ---

    def _validate(self) -> None:
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
        if len(start_edges) != 1:
            raise ValueError("Graph must have exactly one edge from START")

        has_end = (
            any(e.to_node == END for e in self._edges)
            or any(we.to_node == END for we in self._waiting_edges)
            or any(END in ce.mapping.values() for ce in self._conditional_edges)
            or any(END in le.mapping.values() for le in self._llm_edges)
        )
        if not has_end:
            raise ValueError("Graph must have at least one edge to END")

        # NOTE: cycles are intentionally allowed (langgraph model). They are
        # bounded at runtime by ``recursion_limit``; no compile-time check.

    def _validate_params(self) -> None:
        for name, node_def in self._nodes.items():
            for param_name, param_info in node_def.params.items():
                source = param_info["from"]
                if source.startswith("$input."):
                    field_name = source[len("$input."):]
                    if field_name not in self.state_schema.model_fields:
                        raise ValueError(
                            f"Node '{name}' param '{param_name}' references "
                            f"unknown input field: {field_name}"
                        )
                else:
                    ref_node = source.split(".")[0]
                    if ref_node not in self._nodes and ref_node != name:
                        raise ValueError(
                            f"Node '{name}' param '{param_name}' references "
                            f"unknown node: {ref_node}"
                        )

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

    def _get_entry_node(self) -> str:
        return next(e.to_node for e in self._edges if e.from_node == START)

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
                    routes = " | ".join(
                        k if v == END else f"{k}: {v}" for k, v in le.mapping.items()
                    )
                    parts.append(f"─LLM route→ {routes}")

            if not parts:
                parts.append("→ END")
            lines.append(f"  {prefix} {'; '.join(parts)}")

        return "\n".join(lines)
