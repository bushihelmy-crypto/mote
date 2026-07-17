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
        """Orchestrate many tool calls as one graph — map, branch, parallel in one call.

        Orchestrate multiple tool calls as one declarative graph — map
        (batch/fan-out), if/else branching, and parallel execution — in a single
        call.

        Principle: whatever reduces to ``for`` + ``if`` + parallel — a mechanical,
        repetitive workflow — belongs in a graph. When the task is looping over a
        collection, branching on a condition, or firing several independent calls
        at once (``map`` nodes are that ``for``, ``when`` edges are that ``if``,
        and any nodes with no data dependency between them are that parallel —
        they run concurrently by construction), express it as a graph and let this
        tool run it, rather than re-improvising the calls yourself turn by turn. In
        particular, whenever you would emit multiple tool calls in parallel within
        a single turn, put them in one graph instead of scattering them as
        independent calls — the batch is then scheduled, bounded, and reported as
        one unit. The engine guarantees the flow — the loop can't silently skip or
        duplicate items, the branch is evaluated the same way every time, dependent
        steps stay ordered, independent ones run in parallel — so execution is
        reliable and repeatable.

        Prefer it for: parallel tool calls (several INDEPENDENT calls that don't
        depend on each other's results — different tools, or the same tool with
        fixed different args — run them concurrently as sibling nodes with no edge
        between them, instead of emitting N parallel tool calls in one turn);
        fan-out (the SAME tool over every item in a collection, via ``map``); fixed
        pipelines (each step's input is a known function of earlier outputs);
        conditional routing (pick between tools on a prior result); and pure
        data-shaping between steps (filter/reshape/aggregate, via ``compute``).

        Skip it when a single tool call suffices, or when your next step genuinely
        depends on reading/judging an intermediate result before you can decide —
        issue those one turn at a time.

        Submit ``graph`` (a GraphSpec) and ``inputs`` (the values for its declared
        inputs).

        Nodes (``graph.nodes``), each with a unique ``id`` and a ``kind``:
          - "tool":    call a tool — set ``tool`` (its name) and ``args``.
          - "map":     run a tool in parallel over a collection — set ``over`` (the
            list binding), ``as`` (the per-item variable name), and the tool body
            (``tool`` + ``args``, referencing the item as {"$ref": "<as>"}). Its
            result is the list of results. Optional ``concurrency`` (a positive int,
            default 8) caps how many items run at once so a large collection doesn't
            launch every item's tool call simultaneously; raise it for cheap items,
            lower it for heavy ones. Because map items are INDEPENDENT, one item's
            permanent failure is isolated by default (``on_item_error:"skip"``): it
            drops from the result, the rest still run, and the skips are reported
            back to you. (If EVERY item fails the node fails anyway — systematic,
            not isolated.) Set ``on_item_error:"fail"`` to sink the node on any one
            failure.
          - "fold":    map's SERIAL twin — run a tool over a collection ONE ITEM AT
            A TIME, threading an accumulator so each step sees what earlier steps
            built. Set ``over`` + ``as`` (as map), plus ``acc`` (the accumulator
            variable name, read in the body via {"$ref": "<acc>"}), ``initial`` (its
            literal starting value, e.g. {} or [] or 0), and ``reduce`` (how each
            item's result folds into the accumulator: last | append | extend | add |
            or | and | min | max | merge — same vocabulary as a channel). Its result
            is the FINAL accumulator. Use ``fold`` when items DEPEND on each other
            (later items read earlier results — a running glossary, a cumulative
            summary); use ``map`` when they're independent. This replaces the channel
            + guarded back-edge idiom for the common "iterate a known collection
            carrying state" case — reach for a loop (back-edge) only when the number
            of iterations isn't known up front. Because fold items are DEPENDENT,
            ``on_item_error`` defaults to "fail" (a skipped item breaks the chain the
            later ones read); set "skip" only if the accumulation tolerates gaps.
          - "compute": evaluate ``expr`` (a restricted Python expression, or a short
            statement block ending in an expression) over ``args`` (bound as local
            variables). Pure stdlib modules are in scope for data-shaping: re, json,
            math, statistics, itertools, functools, collections, textwrap, string,
            datetime, base64, hashlib (e.g. re.findall(pat, s), json.loads(s),
            collections.Counter(xs)) — already in scope, so ``import`` is unnecessary
            AND fails (writing ``import re`` errors the node). Statement blocks, incl.
            nested for/if, are fine. Unavailable builtins: open, print, type, id, dir,
            input (I/O + introspection are removed) — don't use them, not even to
            debug. No tool call, no file/network. NOTE: use list comprehensions, not
            generator expressions (e.g. sum([x for x in xs]), not sum(x for x in xs)).

        Bindings — any value in ``args`` / ``over`` / ``output`` / a predicate operand:
          - {"$input": "field"}          — a value from ``inputs``.
          - {"$ref": "node"} / {"$ref": "node.key"} — another node's result (a node
            stores its result under its own id), OR a channel's current value (see
            ``channels`` below). Inside a map body, "<as>" is the item.
          - {"$fmt": "template", "<name>": <binding>, ...} — a STRING TEMPLATE: the
            ``$fmt`` value is a Python str.format template and every other key is a
            binding filling one {name} placeholder, e.g.
            {"$fmt": "process {f}", "f": {"$ref": "item"}} → "process foo.txt". Use
            it to splice a value into a string arg (a path, a URL, a command) inline
            instead of adding a compute node just to concatenate. Placeholders are
            {name} (graph-level), unrelated to any shell $var.
          - anything else is a literal (dicts/lists nest bindings).

        Feeding a value into a Bash command (IMPORTANT — a common mistake). A
        binding ($ref/$input) becomes a VALUE in ``args``; it is NOT a shell
        variable. Writing {"command": "process $item"} does NOT interpolate the
        loop item — the shell just sees an unset ``$item``. Bash has a dedicated
        ``inputs`` arg for this: pass {"inputs": {"item": {"$ref": "<as>"}}} and the
        scalar is exported as the env var ``$item`` for the command to use. So a
        map over Bash looks like:
            {"id": "run", "kind": "map", "tool": "Bash", "over": {"$ref": "files"},
             "as": "f",
             "args": {"command": "wc -l \\"$f\\"", "inputs": {"f": {"$ref": "f"}}}}
        (Alternatively, build the command string with a $fmt binding — {"command":
        {"$fmt": "wc -l {f}", "f": {"$ref": "f"}}} — or in a ``compute`` node and
        $ref it into ``command``; but ``inputs`` keeps the command literal and lets
        the shell quote the value.)

        Reading a Bash result — use ``data``, do NOT scrape ``output`` with a loose
        regex. A node reads a tool's STRUCTURED ``data`` (Bash parses JSON stdout
        into it, so ``echo 42`` yields the int 42 and a JSON line yields the parsed
        object); {"$ref": "<node>"} is that clean value. Pulling numbers out of the
        human-readable text with e.g. re.findall(r"\\d+", ...) is fragile: if the
        command FAILED, its error text (exit codes, line numbers) contains stray
        digits that get scraped as if they were results — a silent wrong answer.
        And by default Bash treats a non-zero exit as SUCCESS (its error text rides
        in ``data``/``output``), so a failed item is NOT isolated unless you ask for
        it: pass {"check": true} in the Bash ``args`` so a non-zero exit fails the
        call — then a map/fold ``on_item_error`` isolates the bad item (and reports
        it back) instead of feeding its error text downstream. Leave ``check`` off
        only when a non-zero exit is expected/meaningful (grep with no match, diff
        with changes).

        Channels (``graph.channels``) — mutable loop-carried state. A ``$ref`` names
        one of two things: a NODE RESULT (single-assignment, produced once — consuming
        it adds an automatic edge so the consumer runs after the producer) or a CHANNEL
        (this). A channel is what a loop needs: it has an ``initial`` value so a node
        can read it on the FIRST lap before anything writes it (a node-result ref
        before its producer runs is an error), repeated/parallel writes fold through
        its ``reduce`` (last | append | extend | add | or | and | min | max | merge),
        and — crucially — reading a channel adds NO edge, so a loop body reading state
        seeded by a prior lap is not forced into a spurious ordering. A node writes a
        channel by setting ``writes`` to the channel name (its value is merged via the
        channel's reducer, instead of being stored under the node id). Each channel is
        {"type", "initial", "reduce", "description"}; ``initial`` is a literal (not a
        binding).

        Looping. Execution is langgraph forward-frontier, NOT a static DAG — an edge
        may point BACKWARD, so cycles are allowed and bounded by ``recursion_limit``
        (total node activations across the run; set ``graph.recursion_limit`` to raise
        the budget, default 100, hard cap 10000). For a FIXED collection prefer ``map``
        (independent items, parallel) or ``fold`` (dependent items, serial, carrying an
        accumulator) — neither needs any edge. Reach for a back-edge ``while`` loop ONLY
        when the number of iterations is not known up front (e.g. paginate until no next
        cursor, retry until converged): a channel to carry the loop state, a guarded
        edge from the loop's tail back to its head while the condition holds, and an
        unguarded (else) edge to the exit.

        Edges. By default you do NOT write any — whenever a node's ``args``/``over``
        references another via ``$ref``, an edge is inferred so the referenced node
        runs first, and nodes with no ``$ref`` between them run in parallel. Only add
        explicit ``edges`` in two cases:
          1. Branching — route to different nodes based on a prior result. Add several
             edges out of one node, each with a ``when`` guard; they form an if/elif
             chain (first true wins), and one unguarded edge is the else (optional —
             with no else, an unmatched node just never runs).
          2. Ordering — force node A to run before node B when there is NO ``$ref``
             between them (e.g. B must observe a side effect of A, or B reads a CHANNEL
             A seeded — a channel ref adds no edge, so seed it with an ordering edge).
             Add one unguarded edge {"from": "A", "to": "B"}. The special node
             ``__start__`` is an entry and ``__end__`` is an exit; add
             {"from": "__start__", "to": "X"} to make X a loop's entry when nothing
             else feeds it.
          3. Looping — a guarded edge whose ``to`` points BACK to an earlier node
             repeats it while the guard holds; a sibling unguarded edge to ``__end__``
             (or the next node) is the exit.
        An edge is {"from", "to", and optional "when": a predicate {"left", "op",
        "right"} where left/right are bindings}. Ops: eq, ne, gt, lt, ge, le, in,
        not_in, contains, truthy, falsy.

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
        The unmatched branch's node never runs, so a $ref to it in ``output`` is null.

        Fold example — translate each module in order, carrying a glossary so every
        module reuses the terms chosen by the earlier ones (a serial, order-dependent
        iteration over a FIXED collection — one node, no channel, no back-edge):
        {
          "inputs": {"modules": {"type": "list", "description": "source texts, in order"}},
          "nodes": [
            {"id": "glossary", "kind": "fold", "tool": "TranslateModule",
             "over": {"$input": "modules"}, "as": "m",
             "acc": "acc", "initial": {}, "reduce": "merge",
             "args": {"text": {"$ref": "m"}, "glossary_so_far": {"$ref": "acc"}}}
          ],
          "output": {"$ref": "glossary"}
        }
        The ``fold`` walks ``modules`` one at a time; each call sees the current module
        (``m``) AND the glossary built so far (``acc``), and its result is merged into
        ``acc`` via ``reduce:"merge"``, so the FINAL accumulator (the complete glossary)
        is the node's value. To ACCUMULATE A LIST instead use ``reduce:"append"`` with
        ``initial:[]``; to keep only the last result, ``reduce:"last"``. (When the
        iteration count is NOT known up front — paginate until no next cursor, retry
        until converged — use a channel + guarded back-edge instead, as described under
        Looping above.)

        ``output`` is a binding tree resolved against the final state and returned. A
        ref into a branch that was skipped resolves to null (not an error).

        Hard rules (violate → the call fails):
          - Every {"$input": x} MUST be declared in ``graph.inputs`` first.
          - ``compute`` reads data ONLY via ``args`` (bound as locals) — never embed a
            ``$ref``/``$input`` inside the ``expr`` string; put it in ``args`` and use
            the name.
          - ``output`` is required.

        Minimal example — find every Python file, then grep each of them for a
        pattern in parallel:
        {
          "inputs": {"needle": {"type": "string", "description": "regex to search for"}},
          "nodes": [
            {"id": "files", "kind": "tool", "tool": "Glob", "args": {"pattern": "**/*.py"}},
            {"id": "paths", "kind": "compute", "expr": "text.splitlines()",
             "args": {"text": {"$ref": "files"}}},
            {"id": "hits", "kind": "map", "tool": "Grep",
             "over": {"$ref": "paths"}, "as": "f",
             "args": {"pattern": {"$input": "needle"}, "path": {"$ref": "f"},
                      "output_mode": "content"}}
          ],
          "output": {"$ref": "hits"}
        }
        Called with inputs {"needle": "TODO"} → Glob lists the .py files, compute
        splits that output into a path list, and the map node greps every file for
        "TODO" concurrently, returning the per-file matches.

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
