"""``run_graph`` — orchestrate tools as a declarative graph in one call.

The model submits a :class:`GraphSpec` (nodes + edges) describing how to wire
existing tools together — sequential steps, parallel fan-out (``map``), pure
computation (``compute``), and conditional branching/looping — and this tool
compiles and runs it **foreground**, dispatching each node's tool call back
through the executor chokepoint so permission gating, hooks, and observability
apply unchanged.

Structured, nested parameters → **native tool-use channel only** (the XML
protocol delivers every argument as a string and cannot carry the nested spec).
"""

from __future__ import annotations

from typing import Any

from mote.common.exception.graph import GraphError
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import DispatchTool, ListGraphToolNames, ListToolNames
from mote.executor.tasks.bggraph.from_spec import build_graph, resolve_output
from mote.executor.tasks.bggraph.spec import GraphSpec
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolResult

_MSG_INVALID_SPEC = "Error: invalid graph spec — {error}"
_MSG_COMPILE_FAILED = "Error: could not compile graph — {error}"
_MSG_NESTED_GRAPH = (
    "Error: node {node_id!r} references graph tool {tool!r}. A graph cannot nest "
    "another graph — run graph tools ({tools}) directly, not inside run_graph."
)

RUN_GRAPH_DESCRIPTION = """\
Orchestrate multiple tool calls as one declarative graph — for-loops (map), \
if/else branching, and parallel execution — in a single call, instead of \
issuing the tool calls yourself one turn at a time.

Submit `graph` (a GraphSpec) and `inputs` (the values for its declared inputs).

Nodes (`graph.nodes`), each with a unique `id` and a `kind`:
  - "tool":    call a tool — set `tool` (its name) and `args`.
  - "map":     run a tool in parallel over a collection — set `over` (the list \
binding), `as` (the per-item variable name), and the tool body (`tool` + `args`, \
referencing the item as {"$ref": "<as>"}). Its result is the list of results. \
Optional `concurrency` (a positive int, default 8) caps how many items run at \
once so a large collection doesn't launch every item's tool call \
simultaneously; raise it for cheap items, lower it for heavy ones.
  - "compute": evaluate `expr` (a restricted Python expression, or a short \
statement block ending in an expression) over `args` (bound as local variables). \
Pure stdlib modules are in scope for data-shaping: re, json, math, statistics, \
itertools, functools, collections, textwrap, string, datetime, base64, hashlib \
(e.g. re.findall(pat, s), json.loads(s), collections.Counter(xs)) — already in \
scope, so `import` is unnecessary AND fails (writing `import re` errors the \
node). Statement blocks, incl. nested for/if, are fine. Unavailable builtins: \
open, print, type, id, dir, input (I/O + introspection are removed) — don't use \
them, not even to debug. No tool call, no file/network. NOTE: use list \
comprehensions, not generator expressions (e.g. sum([x for x in xs]), not \
sum(x for x in xs)).

Bindings — any value in `args` / `over` / `output` / a predicate operand:
  - {"$input": "field"}          — a value from `inputs`.
  - {"$ref": "node"} / {"$ref": "node.key"} — another node's result (a node \
stores its result under its own id). Inside a map body, "<as>" is the item.
  - anything else is a literal (dicts/lists nest bindings).

The graph is a DAG — edges only ever flow forward. There is no looping or \
back-edge; to run a step over many items use a `map` node, not a cycle.

Edges are usually inferred automatically from the $refs between nodes; you only \
add `edges` for branching or extra ordering. An edge is {"from", "to", and \
optional "when": a predicate {"left", "op", "right"} where left/right are \
bindings}. Several guarded edges out of one node form an if/elif chain (first \
true wins); an unguarded edge is the else (optional — with none, unmatched \
falls through to the end). Ops: eq, ne, gt, lt, ge, le, in, not_in, contains, \
truthy, falsy.

Branch example — route on a classifier's result (else skips to output):
  "nodes": [
    {"id": "c", "kind": "tool", "tool": "Classify", "args": {"x": {"$input": "v"}}},
    {"id": "big", "kind": "tool", "tool": "Handle", "args": {"s": "yes"}},
    {"id": "small", "kind": "tool", "tool": "Handle", "args": {"s": "no"}}
  ],
  "edges": [
    {"from": "c", "to": "big", "when": {"left": {"$ref": "c"}, "op": "eq", "right": "big"}},
    {"from": "c", "to": "small"}
  ]
The unmatched branch's node never runs, so a $ref to it in `output` is null.

`output` is a binding tree resolved against the final state and returned. A \
ref into a branch that was skipped resolves to null (not an error).

Principles (get these right or the call fails):
  - Every `{"$input": x}` MUST be declared in `graph.inputs` first.
  - `compute` reads data ONLY via `args` (bound as locals) — never embed a \
`$ref`/`$input` inside the `expr` string; put it in `args` and use the name.
  - `output` is required.

Minimal example — double each number, sum them, add 100:
{
  "inputs": {"nums": {"type": "list", "description": "numbers"}},
  "nodes": [
    {"id": "doubled", "kind": "map", "tool": "Double",
     "over": {"$input": "nums"}, "as": "n", "args": {"x": {"$ref": "n"}}},
    {"id": "total", "kind": "compute", "expr": "sum([v for v in vals])",
     "args": {"vals": {"$ref": "doubled"}}},
    {"id": "final", "kind": "tool", "tool": "Add",
     "args": {"a": {"$ref": "total"}, "b": 100}}
  ],
  "output": {"$ref": "final"}
}
Called with inputs {"nums": [1, 2, 3]} → total 12, final 112.\
"""


@register_tool
class RunGraph(BaseTool):
    """Orchestrate tools as a declarative graph (map / branch / parallel) in one call."""

    name = "RunGraph"
    aliases: list[str] = ["run_graph"]
    description = RUN_GRAPH_DESCRIPTION
    requires = ("dispatch_tool", "list_tool_names", "list_graph_tool_names")
    # This tool is itself a graph orchestrator — so it can never be nested.
    is_graph_tool = True

    # Injected from Role by bind():
    dispatch_tool: DispatchTool
    list_tool_names: ListToolNames
    list_graph_tool_names: ListGraphToolNames

    async def call(self, *, graph: GraphSpec, inputs: dict[str, Any] | None = None) -> ToolResult:
        """Compile and run a tool-orchestration graph, returning its resolved output.

        Args:
            graph: the graph spec (nodes + edges + output). See the tool
                description for the node kinds, binding grammar, and edge rules.
            inputs: values for the graph's declared inputs, keyed by input name.

        Runs on the native tool-use channel only (structured, nested params).
        """
        spec = self._parse(graph)

        # Reject graph-in-graph nesting: no node may reference a tool that is
        # itself a graph orchestrator (run_graph, CodeReview, MediaPipeline, ...).
        # This covers run_graph→run_graph recursion AND run_graph→other-graph
        # nesting in one declarative check, before compile. Every other tool
        # (approval-gated, AskUserQuestion, ...) stays orchestratable.
        graph_tools = set(self.list_graph_tool_names())
        for node in spec.nodes:
            if node.kind in ("tool", "map") and node.tool in graph_tools:
                raise ToolError(
                    _MSG_NESTED_GRAPH.format(node_id=node.id, tool=node.tool, tools=", ".join(sorted(graph_tools)))
                )

        # Node tool references are validated against the live tool set at compile
        # time; graph tools are already excluded above.
        valid_tools = set(self.list_tool_names()) - graph_tools

        try:
            compiled = build_graph(
                spec,
                dispatch=self.dispatch_tool,
                command_name=self.name,
                valid_tools=valid_tools,
            )
        except ValueError as exc:
            raise ToolError(_MSG_COMPILE_FAILED.format(error=exc))

        try:
            state = await compiled.arun(**(inputs or {}))
        except GraphError as exc:
            # A node failed / router errored / recursion bound hit. The engine has
            # already pushed per-node notifications; surface a structured failure
            # so the model can inspect and replan (partial state is discarded).
            return ToolResult(output=str(exc), success=False)

        result = resolve_output(spec, state)
        return ToolResult(output=self._format(result), data=result)

    @staticmethod
    def _parse(graph: Any) -> GraphSpec:
        """Validate the raw (native-channel dict) spec into a typed GraphSpec."""
        if isinstance(graph, GraphSpec):
            return graph
        try:
            return GraphSpec.model_validate(graph)
        except Exception as exc:  # noqa: BLE001 — surface a clean failure to the model
            raise ToolError(_MSG_INVALID_SPEC.format(error=exc))

    @staticmethod
    def _format(result: Any) -> str:
        import json

        try:
            return "Graph completed. Output:\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            return f"Graph completed. Output:\n{result!r}"
