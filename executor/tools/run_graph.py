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

import uuid
from typing import Any, ClassVar

from mote.common.events import ActivityCompletedEvent, ActivityStartedEvent, TaskProgressEvent, observe_event_sync
from mote.common.events.scope import ScopeRef, current_scope, push_scope
from mote.common.exception.graph import GraphError
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import DispatchTool, ListGraphExcludedToolNames, ListGraphToolNames, ListToolNames
from mote.executor.tasks.bggraph.from_spec import ItemFailure, build_graph, collect_item_failures, resolve_output
from mote.executor.tasks.bggraph.report import reset_progress_writer, set_progress_writer
from mote.executor.tasks.bggraph.spec import GraphSpec
from mote.executor.tasks.bggraph.types import GraphRunState
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolResult

_MSG_INVALID_SPEC = "Error: invalid graph spec — {error}"
_MSG_COMPILE_FAILED = "Error: could not compile graph — {error}"
_MSG_NESTED_GRAPH = (
    "Error: node {node_id!r} references graph tool {tool!r}. A graph cannot nest "
    "another graph — run graph tools ({tools}) directly, not inside run_graph."
)
_MSG_EXCLUDED_TOOL = (
    "Error: node {node_id!r} references tool {tool!r}, which cannot run inside a "
    "graph. It blocks on an external event (a new message or a background task) "
    "that a foreground graph never delivers, so it would hang the run. Excluded "
    "tools: {tools}. Call it directly, one turn at a time, not inside run_graph."
)


@register_tool
class RunGraph(BaseTool):
    """Orchestrate tools as a declarative graph (map / branch / parallel) in one call."""

    name = "RunGraph"
    aliases: list[str] = ["run_graph"]
    # Recall synonyms for tool-search: ways a model asks to orchestrate multiple
    # steps that the summary does not literally contain.
    keywords: ClassVar[list[str]] = [
        "workflow",
        "orchestrate",
        "pipeline",
        "parallel",
        "for loop",
        "batch",
        "编排",
        "工作流",
        "并行",
        "批量",
        "流水线",
        "循环",
    ]
    requires = ("dispatch_tool", "list_tool_names", "list_graph_tool_names", "list_graph_excluded_tool_names")
    # This tool is itself a graph orchestrator — so it can never be nested.
    is_graph_tool = True

    # Injected from Role by bind():
    dispatch_tool: DispatchTool
    list_tool_names: ListToolNames
    list_graph_tool_names: ListGraphToolNames
    list_graph_excluded_tool_names: ListGraphExcludedToolNames

    async def call(self, *, graph: GraphSpec, inputs: dict[str, Any] | None = None) -> ToolResult:
        """Run a deterministic multi-tool workflow as one foreground graph.

        Use for repetitive or mechanical work: parallel independent calls, a fixed
        pipeline, collection fan-out, conditional routing, or pure data shaping.
        Use ordinary tool calls when one call suffices or the next action requires
        human/model judgment of an intermediate result.

        Node kinds:
          - ``tool``: one tool call (``tool`` + ``args``).
          - ``map``: run a tool concurrently over ``over``; the item is ``$ref`` to
            ``as``. ``concurrency`` defaults to 8. Item errors default to ``skip``;
            all items failing still fails the node.
          - ``fold``: serial ``map`` with an accumulator (``acc``, ``initial``,
            ``reduce``); use when items depend on earlier results. Errors default
            to ``fail``.
          - ``compute``: pure restricted Python over ``args`` locals. Supported
            modules are already in scope; imports, I/O, tools, and generator
            expressions are unavailable.

        Bindings may appear in args, over, output, and predicates:
          - ``{"$input": "x"}``: declared graph input.
          - ``{"$ref": "node"}`` or ``{"$ref": "node.key"}``: node result,
            channel, or map/fold variable.
          - ``{"$fmt": "{x}", "x": <binding>}``: string formatting.
          - Other values are literals; dicts/lists may contain bindings.

        A node-result ``$ref`` automatically orders its consumer after its producer;
        unrelated nodes run in parallel. Usually omit ``edges``. Add edges only for
        ordering without a data dependency, branching (guarded edges are first-match,
        with an optional unguarded else), or cycles. Use ``map``/``fold`` for known
        collections; reserve back-edge loops for unknown iteration counts and bound
        them with ``recursion_limit``.

        Channels carry mutable loop state. They have a literal ``initial`` value and
        a ``reduce`` operation; nodes update them via ``writes``. Reading a channel
        creates no ordering edge, so add an explicit edge when ordering is required.

        Important:
          - Declare every ``$input`` in ``graph.inputs`` and always provide ``output``.
          - ``compute`` receives references through ``args``; never put ``$ref`` or
            ``$input`` syntax inside ``expr``.
          - For Bash, pass bound values through Bash ``inputs`` or ``$fmt``; shell
            ``$var`` text alone does not bind graph values. Use structured ``data``
            downstream, and set ``check:true`` when non-zero exit must fail the item.
          - A reference to a skipped branch resolves to null.

        Args:
            graph: the graph spec (nodes + edges + output).
            inputs: values for the graph's declared inputs, keyed by input name.

        Runs on the native tool-use channel only (structured, nested params).
        """
        spec = self._parse(graph)

        # Reject graph-in-graph nesting: no node may reference a tool that is
        # itself a graph orchestrator (run_graph, CodeReview, ...).
        # This covers run_graph→run_graph recursion AND run_graph→other-graph
        # nesting in one declarative check, before compile. Every other tool
        # (approval-gated, AskUserQuestion, ...) stays orchestratable.
        graph_tools = set(self.list_graph_tool_names())
        excluded_tools = set(self.list_graph_excluded_tool_names())
        for node in spec.nodes:
            if node.kind not in ("tool", "map", "fold"):
                continue
            if node.tool in graph_tools:
                raise ToolError(
                    _MSG_NESTED_GRAPH.format(node_id=node.id, tool=node.tool, tools=", ".join(sorted(graph_tools)))
                )
            # Reject tools that block on an external wake event (Sleep): a
            # foreground graph never delivers one, so the node would hang the run.
            if node.tool in excluded_tools:
                raise ToolError(
                    _MSG_EXCLUDED_TOOL.format(node_id=node.id, tool=node.tool, tools=", ".join(sorted(excluded_tools)))
                )

        # Node tool references are validated against the live tool set at compile
        # time; graph and excluded tools are already rejected above.
        valid_tools = set(self.list_tool_names()) - graph_tools - excluded_tools

        try:
            compiled = build_graph(
                spec,
                dispatch=self.dispatch_tool,
                command_name=self.name,
                valid_tools=valid_tools,
            )
        except ValueError as exc:
            raise ToolError(_MSG_COMPILE_FAILED.format(error=exc))

        # Authoritative per-node record: the graph writes each node's status /
        # attempts / failure text into it as it runs, and the ActivityCompleted
        # outcome tree is read straight off it (self-sufficient — a replay renders
        # the outcome from the terminal event alone, never the live ping stream).
        run_state = GraphRunState.for_graph(compiled)
        topology = self._build_topology(spec)

        # Push a ``graph`` scope so every node (and the tool calls the nodes
        # dispatch) inherits this activity's lineage, and the progress pings the
        # engine emits carry it too. A stable per-call id keys the live widget.
        graph_ref = ScopeRef("graph", uuid.uuid4().hex, "run_graph")

        # Collect per-item map/fold failures (skipped or fatal) so the loss — and
        # the exact args to retry — is surfaced to the model in the tool result:
        # a hard failure otherwise reaches the agent only as the terse "Nodes
        # failed"; the per-item args let it retry the exact call.
        with collect_item_failures() as failures:
            with push_scope(graph_ref) as scope:
                self._emit_started(scope, topology)
                # Foreground ``arun`` installs no progress writer, so the engine's
                # ``report_progress`` calls would go nowhere — wire one whose only
                # sink is the observation bus (task_id="run_graph" so the telemetry
                # mirror fires), carrying the ambient scope so each ping routes into
                # this activity's subtree as a per-node update.
                token = set_progress_writer(self._make_progress_writer())
                try:
                    state = await compiled.arun(run_state=run_state, **(inputs or {}))
                except GraphError as exc:
                    # A node failed / router errored / recursion bound hit. The
                    # engine has recorded per-node outcomes in ``run_state`` and
                    # pushed notifications; freeze the activity to its failure
                    # outcome tree, then surface a structured failure so the model
                    # can inspect and replan (partial state is discarded).
                    self._emit_completed(scope, run_state, "failed", str(exc))
                    return ToolResult(output=str(exc) + self._failure_note(failures), success=False)
                finally:
                    reset_progress_writer(token)

                result = resolve_output(spec, state)
                self._emit_completed(scope, run_state, "success", "")

        # Failure notes ride the output TEXT only — ``data`` stays the clean
        # resolved output so downstream ``$ref``/programmatic reads are unaffected.
        return ToolResult(output=self._format(result) + self._failure_note(failures), data=result)

    # -- activity lineage (the run_graph → node → tool spine) --
    @staticmethod
    def _make_progress_writer():
        """A progress writer whose sole sink is a **scoped** observation ping.

        Foreground ``arun`` installs no writer, so the engine's per-node
        ``report_progress`` calls (RUNNING / retry / cancelled) would go nowhere.
        This writer emits a :class:`TaskProgressEvent` carrying the ambient scope
        (pulled at emit time inside the pushed graph/node scope), so the reducer
        routes each ping into this activity's subtree as a live per-node update.
        Best-effort telemetry — a sink failure never breaks the run.
        """

        def _writer(stage: str, status: Any, detail: Any = None) -> None:
            status_str = status.value if hasattr(status, "value") else str(status)
            detail_str = str(detail) if detail is not None else ""
            sc = current_scope()
            try:
                observe_event_sync(
                    TaskProgressEvent(
                        task_id="run_graph",
                        stage=stage,
                        status=status_str,
                        detail=detail_str,
                        scope=sc,
                    )
                )
            except Exception:  # noqa: BLE001 — telemetry must never break the run
                pass

        return _writer

    def _emit_started(self, scope: tuple, topology: dict) -> None:
        try:
            observe_event_sync(
                ActivityStartedEvent(scope=scope, activity_kind="graph", label="run_graph", topology=topology)
            )
        except Exception:  # noqa: BLE001 — telemetry must never break the run
            pass

    def _emit_completed(self, scope: tuple, run_state: GraphRunState, outcome: str, summary: str) -> None:
        try:
            observe_event_sync(
                ActivityCompletedEvent(
                    scope=scope,
                    outcome=outcome,
                    node_states=self._build_node_states(run_state),
                    summary=summary,
                )
            )
        except Exception:  # noqa: BLE001 — telemetry must never break the run
            pass

    @staticmethod
    def _build_topology(spec: GraphSpec) -> dict:
        """Neutral pre-computed declared shape read by ``activity_topology``.

        ``{"nodes": [{"id","kind","label"}...], "edges": [{"from","to",
        "guarded"}...]}`` — plain dicts so the contracts/render layers import
        nothing from bggraph. ``label`` prefers the node's description.

        Edges mirror what the compiler actually wires: the INFERRED data-flow
        edges (a node's ``$ref`` to another node's result → the referenced node
        runs first — the same derivation ``build_graph`` uses, via ``_node_deps``)
        plus the EXPLICIT ``spec.edges`` (branch/loop routing), the latter marked
        ``guarded`` when they carry a ``when`` predicate. An explicit edge that
        duplicates an inferred one is not double-listed.
        """
        from mote.executor.tasks.bggraph.from_spec import _node_deps

        nodes = [{"id": n.id, "kind": n.kind, "label": (n.description or n.id)} for n in spec.nodes]
        node_ids = {n.id for n in spec.nodes}
        edges: list[dict] = []
        seen: set[tuple[str, str]] = set()
        # Inferred data-flow edges (dep → node) — the implicit dependency spine.
        for node in spec.nodes:
            for dep in _node_deps(node, node_ids):
                pair = (dep, node.id)
                if pair not in seen:
                    seen.add(pair)
                    edges.append({"from": dep, "to": node.id, "guarded": False})
        # Explicit edges (branch / loop routing); guarded when predicated.
        for e in spec.edges:
            pair = (e.from_, e.to)
            if e.when is None and pair in seen:
                continue  # already covered by an inferred data-flow edge
            edges.append({"from": e.from_, "to": e.to, "guarded": e.when is not None})
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _build_node_states(run_state: GraphRunState) -> list[dict]:
        """Neutral per-node outcome list read by ``activity_outcome``.

        ``[{"id","kind","label","status","attempts","error","args"}...]`` off the
        authoritative ``GraphRunState`` records — the self-sufficient terminal
        view (a replay renders from this alone). ``args`` is omitted (the record
        holds no resolved kwargs; per-item retry args ride the failure note).
        """
        states: list[dict] = []
        for name, rec in run_state.records.items():
            states.append(
                {
                    "id": name,
                    "kind": "",
                    "label": name,
                    "status": rec.status.value if hasattr(rec.status, "value") else str(rec.status),
                    "attempts": rec.attempts,
                    "error": rec.last_error or "",
                    "args": None,
                }
            )
        return states

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

    @classmethod
    def _failure_note(cls, failures: list[ItemFailure]) -> str:
        """Render per-item map/fold failures as a terse trailing note.

        Each line carries the tool + resolved args (as JSON) so the model can
        retry that exact call. Grouped by node (named once); each node tagged
        ``skipped`` (dropped, batch continued) or ``failed`` (sank the node).
        Empty → empty string, so a clean run's output is byte-for-byte unchanged.
        """
        if not failures:
            return ""
        import json

        by_node: dict[str, list[ItemFailure]] = {}
        for f in failures:
            by_node.setdefault(f.node, []).append(f)
        lines = [f"\n\n{len(failures)} item(s) failed (tool + args shown so you can retry):"]
        for node, recs in by_node.items():
            verb = "skipped" if recs[0].skipped else "failed"
            lines.append(f"  {node}: {len(recs)} {verb}")
            for r in recs:
                try:
                    args_txt = json.dumps(r.args, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    args_txt = repr(r.args)
                lines.append(f"    - {r.tool}({args_txt}): {r.error}")
        return "\n".join(lines)
