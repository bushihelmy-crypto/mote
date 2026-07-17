#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end tests for the ``run_graph`` orchestrator (mote.executor.tools.run_graph).

``run_graph`` lets the model wire existing tools into a declarative graph —
sequential steps, parallel fan-out (``map``), pure computation (``compute``), and
conditional branching — and runs it in one call, dispatching each node's tool
call back through the executor chokepoint. These tests exercise the full stack
(spec parse → compile → run → dispatch → output resolution) against ``CapRole``'s
scriptable fake tool table (``fake_tools``), so they double as living examples of
every node kind and edge rule the tool description advertises.
"""
from __future__ import annotations

import pytest

from mote.common.events import ACTIVITY_COMPLETED, ACTIVITY_STARTED, TASK_PROGRESS
from mote.common.events.bus import EventBus
from mote.common.events.context import set_bus
from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver
from mote.executor.tool_result import ToolError, ToolResult
from mote.executor.tools.run_graph import RunGraph

from .conftest import CapRole, bind, run


def _role(**tools) -> CapRole:
    """A CapRole whose fake tool table is built from name -> async fn(kwargs)."""
    role = CapRole()
    role.fake_tools = dict(tools)
    return role


def _call(role: CapRole, graph, inputs=None) -> ToolResult:
    tool = bind(RunGraph(), role)
    return run(tool.call(graph=graph, inputs=inputs))


# --- Scriptable fake tools ---------------------------------------------------


async def _double(kw):
    return ToolResult(output=str(kw["x"] * 2), data=kw["x"] * 2)


async def _add(kw):
    return ToolResult(output=str(kw["a"] + kw["b"]), data=kw["a"] + kw["b"])


async def _classify(kw):
    label = "big" if kw["x"] > 5 else "small"
    return ToolResult(output=label, data=label)


async def _shout(kw):
    return ToolResult(output=kw["s"].upper(), data=kw["s"].upper())


async def _deny(kw):
    # Mirrors a denied / failed dispatched call: success=False, never raises.
    return ToolResult(output="denied by user", success=False)


# --- Pipeline: map -> compute -> tool ---------------------------------------


class TestPipeline:
    def test_map_compute_tool_chain(self, workspace):
        role = _role(double=_double, add=_add)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "double",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                },
                {
                    "id": "total",
                    "kind": "compute",
                    "expr": "sum([v for v in vals])",
                    "args": {"vals": {"$ref": "doubled"}},
                },
                {"id": "final", "kind": "tool", "tool": "add", "args": {"a": {"$ref": "total"}, "b": 100}},
            ],
            "output": {"doubled": {"$ref": "doubled"}, "total": {"$ref": "total"}, "final": {"$ref": "final"}},
        }
        result = _call(role, graph, inputs={"nums": [1, 2, 3]})
        assert result.success
        assert result.data == {"doubled": [2, 4, 6], "total": 12, "final": 112}


# --- Map: concurrency cap ----------------------------------------------------


class TestMapConcurrency:
    def _map_graph(self, concurrency=None):
        node = {
            "id": "m",
            "kind": "map",
            "tool": "probe",
            "over": {"$input": "items"},
            "as": "it",
            "args": {"x": {"$ref": "it"}},
        }
        if concurrency is not None:
            node["concurrency"] = concurrency
        return {
            "inputs": {"items": {"type": "list", "description": "items"}},
            "nodes": [node],
            "output": {"$ref": "m"},
        }

    def _run_with_probe(self, graph, n):
        """Run the map with a probe tool that records peak in-flight count."""
        import asyncio

        state = {"cur": 0, "peak": 0}

        async def _probe(kw):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
            state["cur"] -= 1
            return ToolResult(output=str(kw["x"]), data=kw["x"])

        role = _role(probe=_probe)
        r = _call(role, graph, inputs={"items": list(range(n))})
        return r, state["peak"]

    def test_concurrency_caps_in_flight(self, workspace):
        r, peak = self._run_with_probe(self._map_graph(concurrency=2), n=6)
        assert r.success
        assert r.data == list(range(6))  # results stay input-ordered
        assert peak <= 2  # never more than the cap ran at once

    def test_default_caps_at_eight(self, workspace):
        # No explicit concurrency => the default cap (8) applies: 12 items must
        # never exceed 8 in flight.
        r, peak = self._run_with_probe(self._map_graph(concurrency=None), n=12)
        assert r.success
        assert r.data == list(range(12))
        assert peak <= 8
        assert peak > 1  # sanity: items DO overlap (it's not serial)

    def test_concurrency_rejected_on_non_map(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "c", "kind": "compute", "expr": "1", "args": {}, "concurrency": 2}],
            "output": {"$ref": "c"},
        }
        # Spec validation rejects concurrency on a non-map node (raises ToolError).
        with pytest.raises(ToolError, match="must not set 'concurrency'"):
            _call(role, graph)


# --- No graph-in-graph nesting ----------------------------------------------


class TestNoGraphNesting:
    def _role_with_graph_tool(self, name):
        """A role exposing *name* as both a callable fake AND a graph tool."""

        async def _noop(kw):
            return ToolResult(output="ran", data="ran")

        role = _role(**{name: _noop})
        role.graph_tools = {name}
        return role

    def test_tool_node_referencing_graph_tool_rejected(self, workspace):
        role = self._role_with_graph_tool("CodeReview")
        graph = {
            "inputs": {},
            "nodes": [{"id": "n", "kind": "tool", "tool": "CodeReview", "args": {}}],
            "output": {"$ref": "n"},
        }
        with pytest.raises(ToolError, match="cannot nest another graph"):
            _call(role, graph)

    def test_map_node_referencing_graph_tool_rejected(self, workspace):
        role = self._role_with_graph_tool("CodeReview")
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "m",
                    "kind": "map",
                    "tool": "CodeReview",
                    "over": {"$input": "xs"},
                    "as": "x",
                    "args": {},
                }
            ],
            "output": {"$ref": "m"},
        }
        with pytest.raises(ToolError, match="cannot nest another graph"):
            _call(role, graph, inputs={"xs": [1, 2]})

    def test_non_graph_tool_still_allowed(self, workspace):
        # A normal tool alongside a graph tool in the table is unaffected.
        async def _echo(kw):
            return ToolResult(output=kw["s"], data=kw["s"])

        role = _role(echo=_echo)
        role.graph_tools = {"CodeReview"}  # present but unused by the graph
        graph = {
            "inputs": {},
            "nodes": [{"id": "n", "kind": "tool", "tool": "echo", "args": {"s": "hi"}}],
            "output": {"$ref": "n"},
        }
        r = _call(role, graph)
        assert r.success
        assert r.data == "hi"


# --- Excluded tools (Sleep) may not be graph nodes --------------------------


class TestExcludedTools:
    def _role_with_excluded_tool(self, name):
        """A role exposing *name* as both a callable fake AND an excluded tool."""

        async def _noop(kw):
            return ToolResult(output="ran", data="ran")

        role = _role(**{name: _noop})
        role.excluded_tools = {name}
        return role

    def test_tool_node_referencing_excluded_tool_rejected(self, workspace):
        role = self._role_with_excluded_tool("Sleep")
        graph = {
            "inputs": {},
            "nodes": [{"id": "n", "kind": "tool", "tool": "Sleep", "args": {}}],
            "output": {"$ref": "n"},
        }
        with pytest.raises(ToolError, match="cannot run inside a graph"):
            _call(role, graph)

    def test_map_node_referencing_excluded_tool_rejected(self, workspace):
        role = self._role_with_excluded_tool("Sleep")
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "m",
                    "kind": "map",
                    "tool": "Sleep",
                    "over": {"$input": "xs"},
                    "as": "x",
                    "args": {},
                }
            ],
            "output": {"$ref": "m"},
        }
        with pytest.raises(ToolError, match="cannot run inside a graph"):
            _call(role, graph, inputs={"xs": [1, 2]})

    def test_fold_node_referencing_excluded_tool_rejected(self, workspace):
        role = self._role_with_excluded_tool("Sleep")
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "f",
                    "kind": "fold",
                    "tool": "Sleep",
                    "over": {"$input": "xs"},
                    "as": "x",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "add",
                    "args": {},
                }
            ],
            "output": {"$ref": "f"},
        }
        with pytest.raises(ToolError, match="cannot run inside a graph"):
            _call(role, graph, inputs={"xs": [1, 2]})

    def test_non_excluded_tool_still_allowed(self, workspace):
        # A normal tool alongside an excluded tool in the table is unaffected.
        async def _echo(kw):
            return ToolResult(output=kw["s"], data=kw["s"])

        role = _role(echo=_echo)
        role.excluded_tools = {"Sleep"}  # present but unused by the graph
        graph = {
            "inputs": {},
            "nodes": [{"id": "n", "kind": "tool", "tool": "echo", "args": {"s": "hi"}}],
            "output": {"$ref": "n"},
        }
        r = _call(role, graph)
        assert r.success
        assert r.data == "hi"


# --- Compute: curated stdlib namespace + statement blocks --------------------


class TestCompute:
    def _compute(self, expr, args, inputs=None):
        """Run a single compute node and return the resolved output."""
        role = _role()
        graph = {
            "inputs": {k: {"type": "any", "description": k} for k in (inputs or {})},
            "nodes": [{"id": "c", "kind": "compute", "expr": expr, "args": args}],
            "output": {"$ref": "c"},
        }
        return _call(role, graph, inputs=inputs)

    def test_regex_over_text(self, workspace):
        # ``re`` is in scope — no import needed. This is the diff-munging gap the
        # bare asteval sandbox could not cover.
        r = self._compute(
            "re.findall(r'\\+\\+\\+ b/(\\S+)', diff)",
            {"diff": {"$input": "diff"}},
            inputs={"diff": "+++ b/a.py\n+++ b/pkg/b.py\n"},
        )
        assert r.success
        assert r.data == ["a.py", "pkg/b.py"]

    def test_json_roundtrip(self, workspace):
        r = self._compute(
            "json.dumps(json.loads(s), sort_keys=True)",
            {"s": {"$input": "s"}},
            inputs={"s": '{"b": 2, "a": 1}'},
        )
        assert r.data == '{"a": 1, "b": 2}'

    def test_collections_counter(self, workspace):
        r = self._compute(
            "collections.Counter(xs).most_common(1)[0][0]",
            {"xs": {"$input": "xs"}},
            inputs={"xs": ["x", "y", "x", "x"]},
        )
        assert r.data == "x"

    def test_statement_block_ending_in_expr(self, workspace):
        # A short block (assignments + comprehension) whose last node is a bare
        # expression — its value is what the node stores.
        expr = "lines = text.split('\\n')\nkept = [l for l in lines if l.strip()]\nlen(kept)"
        r = self._compute("".join(expr), {"text": {"$input": "text"}}, inputs={"text": "a\n\n b \n\n"})
        assert r.data == 2

    def test_import_still_blocked(self, workspace):
        # The sandbox invariant holds: no ``import`` escape hatch (asteval bars
        # it), so a compute node can never reach os / subprocess / open.
        r = self._compute("__import__('os').getcwd()", {})
        assert r.success is False
        assert "c" in r.output

    def test_file_open_blocked(self, workspace):
        # asteval ships ``open`` in its symtable by default (real file I/O); the
        # lockdown pops it, so a compute node cannot read the filesystem.
        r = self._compute("open('/etc/passwd').read()", {})
        assert r.success is False
        assert "c" in r.output

    def test_while_blocked(self, workspace):
        # ``while`` is disabled via config so an expression cannot spin an
        # unbounded loop (finite comprehensions / ``for`` still work).
        r = self._compute("while True:\n    pass", {})
        assert r.success is False
        assert "c" in r.output

    def test_numpy_builtins_absent(self, workspace):
        # Built with use_numpy=False, so numpy's ~300 builtins (incl. file
        # readers like ``loadtxt`` / ``fromfile``) are not in scope.
        r = self._compute("array([1, 2, 3])", {})
        assert r.success is False
        assert "c" in r.output

    def test_timeout_fails_node(self, workspace, monkeypatch):
        # A runaway expression must fail the node (not freeze the agent). Drive
        # the real wait_for path with a tiny ceiling + a sleeping eval stub.
        import time

        from mote.executor.tasks.bggraph import from_spec

        def _slow(expr, symbols, node_id):
            time.sleep(5)
            return 1

        monkeypatch.setattr(from_spec, "_eval_expr", _slow)
        monkeypatch.setattr(from_spec, "_COMPUTE_TIMEOUT_S", 0.1)
        r = self._compute("1 + 1", {})
        assert r.success is False
        assert "c" in r.output


# --- Branching: guarded edges (if/else) --------------------------------------


class TestBranching:
    def _branch_graph(self):
        return {
            "inputs": {"v": {"type": "integer", "description": "a value"}},
            "nodes": [
                {"id": "c", "kind": "tool", "tool": "classify", "args": {"x": {"$input": "v"}}},
                {"id": "big", "kind": "tool", "tool": "shout", "args": {"s": "yes"}},
                {"id": "small", "kind": "tool", "tool": "shout", "args": {"s": "no"}},
            ],
            "edges": [
                {"from": "c", "to": "big", "when": {"left": {"$ref": "c"}, "op": "eq", "right": "big"}},
                {"from": "c", "to": "small"},  # unguarded == else
            ],
            "output": {"big": {"$ref": "big"}, "small": {"$ref": "small"}},
        }

    def test_true_branch_taken_else_skipped(self, workspace):
        role = _role(classify=_classify, shout=_shout)
        result = _call(role, self._branch_graph(), inputs={"v": 9})
        # big fired; the skipped small branch resolves to None (missing_ok output).
        assert result.data == {"big": "YES", "small": None}

    def test_else_branch_taken(self, workspace):
        role = _role(classify=_classify, shout=_shout)
        result = _call(role, self._branch_graph(), inputs={"v": 2})
        assert result.data == {"big": None, "small": "NO"}


# --- Parallel AND-join -------------------------------------------------------


class TestAndJoin:
    def test_two_deps_join_into_one_node(self, workspace):
        role = _role(double=_double, add=_add)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}, "q": {"type": "integer", "description": "q"}},
            "nodes": [
                {"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}},
                {"id": "dq", "kind": "tool", "tool": "double", "args": {"x": {"$input": "q"}}},
                # depends on both dp and dq -> engine AND-joins them.
                {"id": "sum", "kind": "tool", "tool": "add", "args": {"a": {"$ref": "dp"}, "b": {"$ref": "dq"}}},
            ],
            "output": {"$ref": "sum"},
        }
        result = _call(role, graph, inputs={"p": 2, "q": 5})  # (2*2) + (5*2)
        assert result.data == 14


# --- Failure / denial semantics ----------------------------------------------


class TestFailure:
    def test_denied_tool_call_fails_graph(self, workspace):
        role = _role(boom=_deny)
        graph = {
            "inputs": {},
            "nodes": [{"id": "x", "kind": "tool", "tool": "boom", "args": {}}],
            "output": {"$ref": "x"},
        }
        result = _call(role, graph)
        # A denied/failed dispatch surfaces as a structured failure, not a raise.
        assert result.success is False
        assert "x" in result.output


# --- Optional inputs (required=False) ----------------------------------------


async def _echo_opt(kw):
    # Wraps the received optional value in a marker dict so a resolved None is
    # distinguishable from the tool having failed (_unwrap falls back to .output
    # when .data is None, so we must not return bare None as data).
    return ToolResult(output="ok", data={"got": kw.get("opt")})


class TestOptionalInputs:
    def test_omitted_optional_input_resolves_to_none(self, workspace):
        # A declared optional input (required=False) that the caller omits must
        # resolve to None at {"$input": ...}, not raise GraphToolError.
        role = _role(echo=_echo_opt)
        graph = {
            "inputs": {
                "must": {"type": "string", "description": "required"},
                "opt": {"type": "string", "description": "optional", "required": False},
            },
            "nodes": [
                {"id": "e", "kind": "tool", "tool": "echo", "args": {"opt": {"$input": "opt"}}},
            ],
            "output": {"$ref": "e"},
        }
        result = _call(role, graph, inputs={"must": "hi"})
        assert result.success
        assert result.data == {"got": None}

    def test_provided_optional_input_passes_through(self, workspace):
        role = _role(echo=_echo_opt)
        graph = {
            "inputs": {"opt": {"type": "string", "description": "optional", "required": False}},
            "nodes": [
                {"id": "e", "kind": "tool", "tool": "echo", "args": {"opt": {"$input": "opt"}}},
            ],
            "output": {"$ref": "e"},
        }
        result = _call(role, graph, inputs={"opt": "there"})
        assert result.success
        assert result.data == {"got": "there"}

    def test_missing_required_input_still_fails(self, workspace):
        # Required inputs keep riding extra="allow": omitting one still fails loudly.
        role = _role(echo=_echo_opt)
        graph = {
            "inputs": {"must": {"type": "string", "description": "required"}},
            "nodes": [
                {"id": "e", "kind": "tool", "tool": "echo", "args": {"opt": {"$input": "must"}}},
            ],
            "output": {"$ref": "e"},
        }
        result = _call(role, graph)  # 'must' not provided
        assert result.success is False
        assert "e" in result.output


# --- Compile / validation guards ---------------------------------------------


class TestGuards:
    def test_invalid_spec_rejected(self, workspace):
        role = _role(double=_double)
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, {"nodes": []})  # empty nodes violates min_length

    def test_unknown_tool_rejected_at_compile(self, workspace):
        role = _role(double=_double)  # 'nope' is not in the table
        graph = {
            "inputs": {},
            "nodes": [{"id": "x", "kind": "tool", "tool": "nope", "args": {}}],
            "output": {"$ref": "x"},
        }
        with pytest.raises(ToolError, match="could not compile graph"):
            _call(role, graph)

    def test_cannot_orchestrate_itself(self, workspace):
        # run_graph is itself a graph tool (is_graph_tool=True), so it appears in
        # the graph-tool set and a node naming it is rejected as nesting — the same
        # guard that bars any other graph tool, applied reflexively.
        role = _role()
        role.fake_tools = {"RunGraph": _double, "run_graph": _double}
        role.graph_tools = {"RunGraph", "run_graph"}
        graph = {
            "inputs": {},
            "nodes": [{"id": "x", "kind": "tool", "tool": "run_graph", "args": {}}],
            "output": {"$ref": "x"},
        }
        with pytest.raises(ToolError, match="cannot nest another graph"):
            _call(role, graph)


# --- Channels: loop-carried state (initial value + reducer) ------------------


async def _incr(kw):
    # A stateless "add one" tool used to drive loops/accumulators.
    return ToolResult(output=str(kw["x"] + 1), data=kw["x"] + 1)


async def _identity(kw):
    return ToolResult(output=str(kw["x"]), data=kw["x"])


class TestChannels:
    def test_channel_initial_readable_before_first_write(self, workspace):
        # A node may read a channel on the first lap (its initial value) — a
        # node-result $ref before its producer would instead be a wiring error.
        role = _role(incr=_incr)
        graph = {
            "inputs": {},
            "channels": {"n": {"type": "integer", "initial": 41, "reduce": "last"}},
            "nodes": [
                {"id": "bump", "kind": "tool", "tool": "incr", "args": {"x": {"$ref": "n"}}, "writes": "n"},
            ],
            "edges": [{"from": "__start__", "to": "bump"}],
            "output": {"$ref": "n"},
        }
        r = _call(role, graph)
        assert r.success
        assert r.data == 42  # 41 (initial) + 1, written back into the channel

    def test_reduce_extend_accumulates(self, workspace):
        # A compute node writes into an ``extend`` channel — the reducer grows the
        # list rather than overwriting.
        role = _role()
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "channels": {"acc": {"type": "list", "initial": [], "reduce": "extend"}},
            "nodes": [
                {"id": "a", "kind": "compute", "expr": "[1, 2]", "args": {}, "writes": "acc"},
                {"id": "b", "kind": "compute", "expr": "[3]", "args": {}, "writes": "acc"},
            ],
            # Force a→b ordering so both writes land deterministically.
            "edges": [{"from": "a", "to": "b"}],
            "output": {"$ref": "acc"},
        }
        r = _call(role, graph, inputs={"xs": []})
        assert r.success
        assert r.data == [1, 2, 3]

    def test_reduce_add_running_total(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "channels": {"total": {"type": "integer", "initial": 0, "reduce": "add"}},
            "nodes": [
                {"id": "a", "kind": "compute", "expr": "10", "args": {}, "writes": "total"},
                {"id": "b", "kind": "compute", "expr": "5", "args": {}, "writes": "total"},
            ],
            "edges": [{"from": "a", "to": "b"}],
            "output": {"$ref": "total"},
        }
        r = _call(role, graph)
        assert r.data == 15

    def test_parallel_map_folds_into_channel(self, workspace):
        # map fan-out's result (a list) is extended into an ``extend`` channel.
        role = _role(identity=_identity)
        graph = {
            "inputs": {"items": {"type": "list", "description": "items"}},
            "channels": {"seen": {"type": "list", "initial": [], "reduce": "extend"}},
            "nodes": [
                {
                    "id": "m",
                    "kind": "map",
                    "tool": "identity",
                    "over": {"$input": "items"},
                    "as": "it",
                    "args": {"x": {"$ref": "it"}},
                    "writes": "seen",
                },
            ],
            "output": {"$ref": "seen"},
        }
        r = _call(role, graph, inputs={"items": [1, 2, 3]})
        assert r.success
        assert sorted(r.data) == [1, 2, 3]

    def test_channel_ref_adds_no_edge(self, workspace):
        # Reading a channel must NOT force ordering: two nodes that only share a
        # channel ref (no node-result ref) both run from START. If a spurious
        # edge were added, one would wait on a value never produced and stall.
        role = _role()
        graph = {
            "inputs": {},
            "channels": {"c": {"type": "integer", "initial": 7, "reduce": "last"}},
            "nodes": [
                {"id": "a", "kind": "compute", "expr": "c + 1", "args": {"c": {"$ref": "c"}}},
                {"id": "b", "kind": "compute", "expr": "c + 2", "args": {"c": {"$ref": "c"}}},
            ],
            "output": {"a": {"$ref": "a"}, "b": {"$ref": "b"}},
        }
        r = _call(role, graph)
        assert r.success
        assert r.data == {"a": 8, "b": 9}


# --- Looping: back-edge + recursion_limit ------------------------------------


class TestLooping:
    def test_while_counts_up_to_threshold(self, workspace):
        # Classic while: increment a counter channel until it reaches 3, looping
        # via a guarded back-edge; exit via the else edge to __end__.
        role = _role(incr=_incr)
        graph = {
            "inputs": {},
            "channels": {"i": {"type": "integer", "initial": 0, "reduce": "last"}},
            "nodes": [
                {"id": "step", "kind": "tool", "tool": "incr", "args": {"x": {"$ref": "i"}}, "writes": "i"},
            ],
            "edges": [
                {"from": "__start__", "to": "step"},
                {"from": "step", "to": "step", "when": {"left": {"$ref": "i"}, "op": "lt", "right": 3}},
                {"from": "step", "to": "__end__"},
            ],
            "output": {"$ref": "i"},
        }
        r = _call(role, graph)
        assert r.success
        assert r.data == 3

    def test_recursion_limit_fails_runaway_loop(self, workspace):
        # A back-edge whose guard is always true is an infinite loop; the
        # recursion_limit must bound it and surface a structured failure (not
        # hang, not raise).
        role = _role(incr=_incr)
        graph = {
            "inputs": {},
            "channels": {"i": {"type": "integer", "initial": 0, "reduce": "last"}},
            "nodes": [
                {"id": "step", "kind": "tool", "tool": "incr", "args": {"x": {"$ref": "i"}}, "writes": "i"},
            ],
            "edges": [
                {"from": "__start__", "to": "step"},
                {"from": "step", "to": "step", "when": {"left": {"$ref": "i"}, "op": "ge", "right": 0}},
                {"from": "step", "to": "__end__"},
            ],
            "recursion_limit": 5,
            "output": {"$ref": "i"},
        }
        r = _call(role, graph)
        assert r.success is False


# --- Channel / writes validation guards --------------------------------------


class TestChannelGuards:
    def test_writes_to_undeclared_channel_rejected(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "a", "kind": "compute", "expr": "1", "args": {}, "writes": "nope"}],
            "output": {"$ref": "a"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)

    def test_channel_name_collides_with_node_id_rejected(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "channels": {"a": {"type": "integer", "initial": 0}},
            "nodes": [{"id": "a", "kind": "compute", "expr": "1", "args": {}}],
            "output": {"$ref": "a"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)

    def test_channel_name_collides_with_input_rejected(self, workspace):
        role = _role()
        graph = {
            "inputs": {"v": {"type": "integer", "description": "v"}},
            "channels": {"v": {"type": "integer", "initial": 0}},
            "nodes": [{"id": "a", "kind": "compute", "expr": "1", "args": {}}],
            "output": {"$ref": "a"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)

    def test_ref_to_unknown_channel_rejected_at_compile(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "a", "kind": "compute", "expr": "x", "args": {"x": {"$ref": "ghost"}}}],
            "output": {"$ref": "a"},
        }
        with pytest.raises(ToolError, match="could not compile graph"):
            _call(role, graph)


# --- Fold: map's serial twin (order-dependent iteration with an accumulator) --


async def _translate(kw):
    # Translate one module name; the running glossary is passed in so a later
    # item's "translation" can depend on what earlier items produced (order
    # matters). Returns a one-entry dict to be merged into the accumulator.
    glossary = kw["glossary_so_far"]
    term = kw["term"]
    # Deterministic "translation": index within the accumulated glossary, so the
    # value proves each step saw the state built by the previous ones.
    return ToolResult(output=term, data={term: f"t{len(glossary)}"})


async def _acc_sum(kw):
    # Add the item to the running total (proves acc + item both reach the body).
    return ToolResult(output="", data=kw["acc"] + kw["item"])


class TestFold:
    def test_merge_accumulates_and_sees_prior(self, workspace):
        # The canonical fold: build a dict by merging each item's result, with
        # each step reading the accumulator built so far. This is exactly the
        # "sequential fold" that used to need a channel + guarded back-edge.
        role = _role(translate=_translate)
        graph = {
            "inputs": {"mods": {"type": "list", "description": "module names"}},
            "nodes": [
                {
                    "id": "glossary",
                    "kind": "fold",
                    "tool": "translate",
                    "over": {"$input": "mods"},
                    "as": "m",
                    "acc": "acc",
                    "initial": {},
                    "reduce": "merge",
                    "args": {"term": {"$ref": "m"}, "glossary_so_far": {"$ref": "acc"}},
                },
            ],
            "output": {"$ref": "glossary"},
        }
        r = _call(role, graph, inputs={"mods": ["a", "b", "c"]})
        assert r.success
        # Each value is the glossary size at that step → proves serial ordering
        # and that the accumulator was visible to the body.
        assert r.data == {"a": "t0", "b": "t1", "c": "t2"}

    def test_add_running_total(self, workspace):
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "total",
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",  # body returns the new running total directly
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "total"},
        }
        r = _call(role, graph, inputs={"xs": [1, 2, 3, 4]})
        assert r.success
        assert r.data == 10

    def test_empty_collection_yields_initial(self, workspace):
        # No items → the accumulator is never touched → its initial is returned.
        role = _role(translate=_translate)
        graph = {
            "inputs": {"mods": {"type": "list", "description": "module names"}},
            "nodes": [
                {
                    "id": "glossary",
                    "kind": "fold",
                    "tool": "translate",
                    "over": {"$input": "mods"},
                    "as": "m",
                    "acc": "acc",
                    "initial": {"seed": "x"},
                    "reduce": "merge",
                    "args": {"term": {"$ref": "m"}, "glossary_so_far": {"$ref": "acc"}},
                },
            ],
            "output": {"$ref": "glossary"},
        }
        r = _call(role, graph, inputs={"mods": []})
        assert r.success
        assert r.data == {"seed": "x"}

    def test_writes_into_channel(self, workspace):
        # A fold may redirect its final accumulator into a channel via ``writes``
        # (the general node-sink mechanism), same as any other node kind.
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "numbers"}},
            "channels": {"out": {"type": "integer", "initial": -1, "reduce": "last"}},
            "nodes": [
                {
                    "id": "total",
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                    "writes": "out",
                },
            ],
            "output": {"$ref": "out"},
        }
        r = _call(role, graph, inputs={"xs": [2, 3]})
        assert r.success
        assert r.data == 5

    def test_failed_item_fails_fold(self, workspace):
        # A denied/failed dispatched call in the body fails the whole fold node.
        role = _role(deny=_deny)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "f",
                    "kind": "fold",
                    "tool": "deny",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",
                    "args": {"x": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "f"},
        }
        r = _call(role, graph, inputs={"xs": [1, 2]})
        assert r.success is False

    def test_over_must_be_list(self, workspace):
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"x": {"type": "integer", "description": "not a list"}},
            "nodes": [
                {
                    "id": "f",
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "x"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "f"},
        }
        r = _call(role, graph, inputs={"x": 5})
        assert r.success is False

    def test_missing_acc_rejected(self, workspace):
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "f",
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "initial": 0,
                    "args": {"item": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "f"},
        }
        with pytest.raises(ToolError, match="requires 'acc'"):
            _call(role, graph, inputs={"xs": [1]})

    def test_as_and_acc_must_differ(self, workspace):
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "f",
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "xs"},
                    "as": "x",
                    "acc": "x",
                    "initial": 0,
                    "args": {"item": {"$ref": "x"}},
                },
            ],
            "output": {"$ref": "f"},
        }
        with pytest.raises(ToolError, match="must be different names"):
            _call(role, graph, inputs={"xs": [1]})

    def test_reduce_rejected_on_non_fold(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "c", "kind": "compute", "expr": "1", "args": {}, "reduce": "add"}],
            "output": {"$ref": "c"},
        }
        with pytest.raises(ToolError, match="must not set 'reduce'"):
            _call(role, graph)

    def test_initial_rejected_on_non_fold(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "c", "kind": "compute", "expr": "1", "args": {}, "initial": 5}],
            "output": {"$ref": "c"},
        }
        with pytest.raises(ToolError, match="must not set 'initial'"):
            _call(role, graph)

    def test_acc_ref_adds_no_spurious_dep(self, workspace):
        # The accumulator name is loop-local: a $ref to it must NOT be treated as
        # a dependency on another node (which would stall waiting on a value never
        # produced). A lone fold reading its own acc runs straight from START.
        role = _role(acc_sum=_acc_sum)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "acc",  # deliberately shares the accumulator var name
                    "kind": "fold",
                    "tool": "acc_sum",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "acc"},
        }
        r = _call(role, graph, inputs={"xs": [1, 2, 3]})
        assert r.success
        assert r.data == 6


# --- on_item_error: per-item isolation for map / fold ------------------------


async def _double_but_deny_odd(kw):
    # A tool that fails permanently on odd inputs (mirrors a denied/failed call)
    # and doubles even ones — lets one item in a batch fail deterministically.
    x = kw["x"]
    if x % 2 == 1:
        return ToolResult(output="denied by user", success=False)
    return ToolResult(output=str(x * 2), data=x * 2)


async def _add_but_deny_negative(kw):
    # A fold body that fails on a negative item and otherwise adds it to the acc.
    item = kw["item"]
    if item < 0:
        return ToolResult(output="denied by user", success=False)
    return ToolResult(output="", data=kw["acc"] + item)


class TestOnItemError:
    def test_map_skip_omits_failed_keeps_rest(self, workspace):
        # skip mode: the odd items (1, 3) fail and drop out of the result list;
        # the even ones (2, 4) survive, doubled. One bad item does not sink the
        # batch, and the survivors stay in input order.
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "skip",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2, 3, 4]})
        assert r.success
        assert r.data == [4, 8]  # only the even items, doubled, in order

    def test_map_skip_surfaces_note_to_model(self, workspace):
        # The skipped items must not be silent: the tool output carries a note so
        # the model learns which items were dropped and why.
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "skip",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2, 3]})
        assert r.success
        assert "2 item(s) failed" in r.output
        assert "doubled: 2 skipped" in r.output  # node named once, skip-tagged, grouped
        assert "denied by user" in r.output  # the bare reason (boilerplate stripped)
        # The resolved call (tool + args) is shown so the model can retry it.
        assert 'dbl({"x": 1})' in r.output
        assert 'dbl({"x": 3})' in r.output
        # ``data`` stays the clean resolved output — the note is text-only.
        assert r.data == [4]

    def test_map_defaults_to_skip(self, workspace):
        # map items are INDEPENDENT, so the default (no on_item_error) is "skip":
        # one bad item drops out and the rest survive — a batch of dozens is not
        # sunk by a single failure.
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2, 3, 4]})
        assert r.success
        assert r.data == [4, 8]  # odd items skipped by default
        assert "2 item(s) failed" in r.output
        assert "doubled: 2 skipped" in r.output

    def test_map_all_items_failed_still_fails(self, workspace):
        # All-failed guard: under skip, a non-empty input where EVERY item fails is
        # a systematic error (wrong arg, etc.), not an isolated one — the node must
        # fail loudly rather than silently returning an empty list.
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "skip",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [1, 3, 5]})  # all odd → all fail
        assert r.success is False
        # The node failed (all-failed guard tripped); the engine reports the failed
        # node and the per-item failures still ride out with their args.
        assert "doubled" in r.output
        assert "3 item(s) failed" in r.output

    def test_map_explicit_fail_propagates(self, workspace):
        # Explicit on_item_error="fail" overrides the skip default: one failed item
        # sinks the whole node (all-or-nothing).
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "fail",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2, 3, 4]})
        assert r.success is False

    def test_clean_run_has_no_skip_note(self, workspace):
        # No item fails → no note appended; output is the plain completion text.
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "skip",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [2, 4, 6]})
        assert r.success
        assert r.data == [4, 8, 12]
        assert "item(s) failed" not in r.output

    def test_fold_skip_does_not_fold_failed_item(self, workspace):
        # skip mode on a fold: the negative item (-5) fails and is NOT folded into
        # the accumulator; the running total is over the survivors only.
        role = _role(add=_add_but_deny_negative)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "total",
                    "kind": "fold",
                    "tool": "add",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                    "on_item_error": "skip",
                },
            ],
            "output": {"$ref": "total"},
        }
        r = _call(role, graph, inputs={"xs": [1, 2, -5, 3]})
        assert r.success
        assert r.data == 6  # 1 + 2 + 3; the -5 item was skipped, not folded
        assert "1 item(s) failed" in r.output
        assert "total: 1 skipped" in r.output
        # The failed item's resolved args (item + the acc it saw, =3) are shown.
        assert '"item": -5' in r.output

    def test_fold_default_fail_propagates(self, workspace):
        # Default "fail" on a fold: the failed item sinks the whole node.
        role = _role(add=_add_but_deny_negative)
        graph = {
            "inputs": {"xs": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "total",
                    "kind": "fold",
                    "tool": "add",
                    "over": {"$input": "xs"},
                    "as": "it",
                    "acc": "acc",
                    "initial": 0,
                    "reduce": "last",
                    "args": {"acc": {"$ref": "acc"}, "item": {"$ref": "it"}},
                },
            ],
            "output": {"$ref": "total"},
        }
        r = _call(role, graph, inputs={"xs": [1, -5, 3]})
        assert r.success is False
        # A fatal per-item failure still surfaces the tool + resolved args (the item
        # and the acc it saw) so the model can retry the exact failing call.
        assert "1 item(s) failed" in r.output
        assert "total: 1 failed" in r.output  # tagged fatal, not skipped
        assert '"item": -5' in r.output

    def test_map_fatal_failure_surfaces_args_to_retry(self, workspace):
        # A fatal map failure (on_item_error="fail") must also carry the failed
        # item's tool + resolved args out, so the model can retry the exact call —
        # not just learn the node id from "Nodes failed".
        role = _role(dbl=_double_but_deny_odd)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "doubled",
                    "kind": "map",
                    "tool": "dbl",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                    "on_item_error": "fail",
                },
            ],
            "output": {"$ref": "doubled"},
        }
        r = _call(role, graph, inputs={"nums": [2, 3, 4]})  # 3 is odd → fatal
        assert r.success is False
        assert "1 item(s) failed" in r.output
        assert "doubled: 1 failed" in r.output
        assert 'dbl({"x": 3})' in r.output  # the exact call to retry

    def test_on_item_error_rejected_on_tool_node(self, workspace):
        role = _role(dbl=_double)
        graph = {
            "inputs": {},
            "nodes": [{"id": "x", "kind": "tool", "tool": "dbl", "args": {"x": 1}, "on_item_error": "skip"}],
            "output": {"$ref": "x"},
        }
        with pytest.raises(ToolError, match="must not set 'on_item_error'"):
            _call(role, graph)

    def test_on_item_error_rejected_on_compute_node(self, workspace):
        role = _role()
        graph = {
            "inputs": {},
            "nodes": [{"id": "c", "kind": "compute", "expr": "1", "args": {}, "on_item_error": "skip"}],
            "output": {"$ref": "c"},
        }
        with pytest.raises(ToolError, match="must not set 'on_item_error'"):
            _call(role, graph)


# --- Activity lineage: the run_graph → node → tool provenance spine ----------
#
# W1/W2 wire run_graph into the Activity view protocol: the tool emits an
# ActivityStarted (declared topology) before the run, per-node TaskProgress pings
# while it runs (via the foreground progress writer W2 installs), and a
# self-sufficient ActivityCompleted (outcome tree read off GraphRunState) after.
# The graph scope pushed around the run also makes every dispatched node tool
# call carry a non-empty ``current_scope()`` — the mechanism that fixes orphan
# child rows (graph-internal calls whose PreToolUse/PostToolUse would otherwise
# render top-level). These tests bind a capturing observer to the event bus for
# the duration of the call and assert on what the spine emitted.


class _CaptureObserver(ObservationSubscriber, SyncObserver):
    """Collects every observation event (both async and sync) for assertions."""

    def __init__(self) -> None:
        self.events: list = []

    async def handle(self, event) -> None:
        self.events.append(event)

    def handle_sync(self, event) -> None:
        self.events.append(event)

    def of(self, name: str) -> list:
        return [e for e in self.events if getattr(e, "name", None) == name]


def _call_capturing(role: CapRole, graph, inputs=None):
    """Run the graph under a live bus, returning ``(result, observer)``."""
    bus = EventBus()
    obs = _CaptureObserver()
    bus.subscribe(obs)
    tool = bind(RunGraph(), role)

    async def _driven():
        with set_bus(bus):
            return await tool.call(graph=graph, inputs=inputs)

    return run(_driven()), obs


class TestActivityLineage:
    def test_emits_started_topology_matching_spec(self, workspace):
        role = _role(double=_double, add=_add)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}},
            "nodes": [
                {"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}},
                {"id": "sum", "kind": "tool", "tool": "add", "args": {"a": {"$ref": "dp"}, "b": 1}},
            ],
            "output": {"$ref": "sum"},
        }
        result, obs = _call_capturing(role, graph, inputs={"p": 3})
        assert result.success

        started = obs.of(ACTIVITY_STARTED)
        assert len(started) == 1
        ev = started[0]
        assert ev.activity_kind == "graph"
        assert ev.label == "run_graph"
        assert ev.scope != ()  # the pushed graph scope
        # Topology mirrors the declared spec: one node dict per spec node, with
        # the inferred dp→sum data-flow edge (dp is referenced by sum's args).
        node_ids = {n["id"] for n in ev.topology["nodes"]}
        assert node_ids == {"dp", "sum"}
        assert {n["kind"] for n in ev.topology["nodes"]} == {"tool"}
        assert {(e["from"], e["to"]) for e in ev.topology["edges"]} == {("dp", "sum")}
        assert all(e["guarded"] is False for e in ev.topology["edges"])

    def test_emits_completed_node_states_matching_run_state(self, workspace):
        role = _role(double=_double, add=_add)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}},
            "nodes": [
                {"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}},
                {"id": "sum", "kind": "tool", "tool": "add", "args": {"a": {"$ref": "dp"}, "b": 1}},
            ],
            "output": {"$ref": "sum"},
        }
        result, obs = _call_capturing(role, graph, inputs={"p": 3})
        assert result.success

        completed = obs.of(ACTIVITY_COMPLETED)
        assert len(completed) == 1
        ev = completed[0]
        assert ev.outcome == "success"
        by_id = {s["id"]: s for s in ev.node_states}
        assert set(by_id) == {"dp", "sum"}
        # A clean run leaves every node SUCCESS with no error text.
        assert all(s["status"] == "success" for s in by_id.values())
        assert all(s["error"] == "" for s in by_id.values())

    def test_emits_per_node_progress_pings(self, workspace):
        role = _role(double=_double, add=_add)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}},
            "nodes": [
                {"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}},
                {"id": "sum", "kind": "tool", "tool": "add", "args": {"a": {"$ref": "dp"}, "b": 1}},
            ],
            "output": {"$ref": "sum"},
        }
        result, obs = _call_capturing(role, graph, inputs={"p": 3})
        assert result.success

        # W2's foreground writer turns the engine's report_progress(RUNNING) calls
        # into scoped TaskProgress pings — one per node, each carrying the graph
        # scope so the reducer routes it into this activity's subtree.
        pings = obs.of(TASK_PROGRESS)
        stages = {p.stage for p in pings}
        assert {"dp", "sum"} <= stages
        assert all(p.scope != () for p in pings)
        assert all(p.scope[0].kind == "graph" for p in pings)

    def test_dispatched_tool_calls_carry_non_empty_scope(self, workspace):
        # The orphan-fix mechanism: a node body pushes a ``node`` scope, so the
        # tool call it dispatches sees a non-empty ``current_scope()`` whose head
        # is the graph and tail is the node. (In production this scope stamps the
        # PreToolUse/PostToolUse events, nesting the child under the activity
        # instead of orphaning it top-level.)
        seen: dict[str, tuple] = {}

        async def _record_scope(kw):
            from mote.common.events.scope import current_scope

            seen[kw["tag"]] = current_scope()
            return ToolResult(output="ok", data="ok")

        role = _role(rec=_record_scope)
        graph = {
            "inputs": {},
            "nodes": [
                {"id": "first", "kind": "tool", "tool": "rec", "args": {"tag": "first"}},
                {"id": "second", "kind": "tool", "tool": "rec", "args": {"tag": "second"}},
            ],
            "output": {"$ref": "second"},
        }
        result, _obs = _call_capturing(role, graph)
        assert result.success
        for tag, node_id in (("first", "first"), ("second", "second")):
            scope = seen[tag]
            assert scope != ()
            assert scope[0].kind == "graph"
            assert scope[-1].kind == "node"
            assert scope[-1].id == node_id

    def test_map_children_inherit_scope(self, workspace):
        # A map fans out concurrent dispatches; each gathered child coroutine must
        # inherit the ambient (graph, node) scope via the contextvar copy.
        seen: list[tuple] = []

        async def _record(kw):
            from mote.common.events.scope import current_scope

            seen.append(current_scope())
            return ToolResult(output=str(kw["x"]), data=kw["x"])

        role = _role(rec=_record)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "nums"}},
            "nodes": [
                {
                    "id": "m",
                    "kind": "map",
                    "tool": "rec",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {"x": {"$ref": "n"}},
                }
            ],
            "output": {"$ref": "m"},
        }
        result, _obs = _call_capturing(role, graph, inputs={"nums": [1, 2, 3]})
        assert result.success
        assert len(seen) == 3
        for scope in seen:
            assert scope != ()
            assert scope[0].kind == "graph"
            assert scope[-1].kind == "node"
            assert scope[-1].id == "m"

    def test_graph_error_still_emits_completed_with_failure(self, workspace):
        role = _role(boom=_deny)
        graph = {
            "inputs": {},
            "nodes": [{"id": "x", "kind": "tool", "tool": "boom", "args": {}}],
            "output": {"$ref": "x"},
        }
        result, obs = _call_capturing(role, graph)
        assert result.success is False

        # The failure path must still freeze the activity to a self-sufficient
        # outcome tree (a replay renders the failure from this event alone).
        completed = obs.of(ACTIVITY_COMPLETED)
        assert len(completed) == 1
        ev = completed[0]
        assert ev.outcome == "failed"
        assert ev.summary  # carries the GraphError text
        by_id = {s["id"]: s for s in ev.node_states}
        assert "x" in by_id
        assert by_id["x"]["status"] == "failed"

    def test_started_precedes_completed(self, workspace):
        role = _role(double=_double)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}},
            "nodes": [{"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}}],
            "output": {"$ref": "dp"},
        }
        _result, obs = _call_capturing(role, graph, inputs={"p": 2})
        names = [getattr(e, "name", None) for e in obs.events]
        assert names.index(ACTIVITY_STARTED) < names.index(ACTIVITY_COMPLETED)

    def test_no_bus_bound_still_runs(self, workspace):
        # The emit helpers are best-effort: with no bus bound the events drop
        # silently and the graph still completes normally.
        role = _role(double=_double)
        graph = {
            "inputs": {"p": {"type": "integer", "description": "p"}},
            "nodes": [{"id": "dp", "kind": "tool", "tool": "double", "args": {"x": {"$input": "p"}}}],
            "output": {"$ref": "dp"},
        }
        result = _call(role, graph, inputs={"p": 5})  # plain path, no bus
        assert result.success
        assert result.data == 10


# --- Bash inside a map: shell values via `inputs`, failures via `check` -------
#
# These double as the canonical examples for the two run_graph-with-Bash
# pitfalls the tool description warns about:
#   (1) a per-item loop value reaches the shell via Bash's ``inputs`` (a binding),
#       NOT by writing ``$item`` in the command string (the graph never injects
#       shell env vars);
#   (2) a failed command is isolated by ``on_item_error`` ONLY when the model
#       passes ``check: true`` — because Bash treats a non-zero exit as success by
#       default, and its structured ``data`` (not scraped text) is the clean value.


async def _fake_bash(kw):
    """A minimal stand-in mirroring the real Bash contract for these examples.

    Reads the item from ``inputs`` (as Bash exports scalars to env vars), returns
    the doubled value as structured ``data`` (as Bash parses JSON stdout), and —
    when ``check`` is on and the item is odd — returns ``success=False`` (as Bash
    fails a non-zero exit under check). Without ``check`` an odd item "fails" but
    still reports ``success=True`` with error TEXT, reproducing the contamination
    trap.
    """
    n = kw["inputs"]["n"]
    if n % 2 == 1:
        if kw.get("check"):
            return ToolResult(output=f"Command failed with exit code 1.\nbad {n}", success=False)
        # Non-zero exit WITHOUT check: success=True, error text rides in output/data.
        return ToolResult(output=f"error: bad {n} [exit code: 1]", data=f"error: bad {n} [exit code: 1]")
    return ToolResult(output=str(n * 2), data=n * 2)


class TestBashInMap:
    def test_loop_value_reaches_shell_via_inputs(self, workspace):
        # The per-item value flows into the command through ``inputs`` (a binding),
        # so the clean structured data is the result — no shell $var injection, no
        # regex scraping. Even items double; here all inputs are even.
        role = _role(Bash=_fake_bash)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "run",
                    "kind": "map",
                    "tool": "Bash",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {
                        "command": 'echo "$((n * 2))"',
                        "inputs": {"n": {"$ref": "n"}},
                    },
                },
            ],
            "output": {"$ref": "run"},
        }
        r = _call(role, graph, inputs={"nums": [2, 4, 6]})
        assert r.success
        assert r.data == [4, 8, 12]  # clean structured ints, not scraped text

    def test_check_isolates_failed_command(self, workspace):
        # With check, an odd item's non-zero exit becomes success=False, so the
        # map isolates it via on_item_error and the clean survivors remain — the
        # error text never contaminates the result list.
        role = _role(Bash=_fake_bash)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "run",
                    "kind": "map",
                    "tool": "Bash",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {
                        "command": 'echo "$((n * 2))"',
                        "inputs": {"n": {"$ref": "n"}},
                        "check": True,
                    },
                },
            ],
            "output": {"$ref": "run"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2, 3, 4]})
        assert r.success
        assert r.data == [4, 8]  # only the even items; odd ones isolated, not scraped
        assert "2 item(s) failed" in r.output  # the skips are surfaced

    def test_without_check_error_text_contaminates(self, workspace):
        # WITHOUT check, a failed command reports success=True with its error text,
        # so it lands in the result list looking like a value. This is the trap the
        # description warns about — asserted here so the contrast is explicit.
        role = _role(Bash=_fake_bash)
        graph = {
            "inputs": {"nums": {"type": "list", "description": "numbers"}},
            "nodes": [
                {
                    "id": "run",
                    "kind": "map",
                    "tool": "Bash",
                    "over": {"$input": "nums"},
                    "as": "n",
                    "args": {
                        "command": 'echo "$((n * 2))"',
                        "inputs": {"n": {"$ref": "n"}},
                    },
                },
            ],
            "output": {"$ref": "run"},
        }
        r = _call(role, graph, inputs={"nums": [1, 2]})
        assert r.success
        # The odd item's error text is in the list — garbage the model must not
        # scrape. The fix is ``check: true`` (previous test).
        assert r.data == ["error: bad 1 [exit code: 1]", 4]


# --- $fmt: string-template binding ------------------------------------------


async def _echo_cmd(kw):
    """Return the ``command`` arg verbatim so a test can inspect the formatted string."""
    return ToolResult(output=kw["command"], data=kw["command"])


class TestFmtBinding:
    def test_fmt_fills_from_input(self, workspace):
        # A $fmt template interpolates a $input-bound value into a literal string.
        role = _role(sh=_echo_cmd)
        graph = {
            "inputs": {"path": {"type": "string", "description": "a file"}},
            "nodes": [
                {
                    "id": "cmd",
                    "kind": "tool",
                    "tool": "sh",
                    "args": {"command": {"$fmt": "wc -l {f}", "f": {"$input": "path"}}},
                },
            ],
            "output": {"$ref": "cmd"},
        }
        r = _call(role, graph, inputs={"path": "notes.txt"})
        assert r.success
        assert r.data == "wc -l notes.txt"

    def test_fmt_per_item_in_map(self, workspace):
        # In a map, $fmt interpolates the per-item loop var into each command — the
        # tool-agnostic alternative to a compute node that just concatenates.
        role = _role(sh=_echo_cmd)
        graph = {
            "inputs": {"files": {"type": "list", "description": "files"}},
            "nodes": [
                {
                    "id": "cmds",
                    "kind": "map",
                    "tool": "sh",
                    "over": {"$input": "files"},
                    "as": "f",
                    "args": {"command": {"$fmt": "cat {name}", "name": {"$ref": "f"}}},
                },
            ],
            "output": {"$ref": "cmds"},
        }
        r = _call(role, graph, inputs={"files": ["a.txt", "b.txt"]})
        assert r.success
        assert r.data == ["cat a.txt", "cat b.txt"]

    def test_fmt_sibling_creates_dataflow_edge(self, workspace):
        # A $ref inside a $fmt sibling must add a data-flow edge so the producer
        # runs first — otherwise the consumer would format against a missing value.
        role = _role(double=_double, sh=_echo_cmd)
        graph = {
            "inputs": {"x": {"type": "integer", "description": "seed"}},
            "nodes": [
                {"id": "d", "kind": "tool", "tool": "double", "args": {"x": {"$input": "x"}}},
                {
                    "id": "cmd",
                    "kind": "tool",
                    "tool": "sh",
                    # Reads node 'd's result inside the template; the edge d->cmd is
                    # implied by this $ref, so d runs before cmd.
                    "args": {"command": {"$fmt": "echo {v}", "v": {"$ref": "d"}}},
                },
            ],
            "output": {"$ref": "cmd"},
        }
        r = _call(role, graph, inputs={"x": 21})
        assert r.success
        assert r.data == "echo 42"

    def test_fmt_cannot_mix_with_ref(self, workspace):
        role = _role(sh=_echo_cmd)
        graph = {
            "inputs": {},
            "nodes": [
                {"id": "cmd", "kind": "tool", "tool": "sh", "args": {"command": {"$fmt": "x", "$ref": "n"}}},
            ],
            "output": {"$ref": "cmd"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)

    def test_fmt_missing_sibling_rejected(self, workspace):
        # A {name} placeholder with no matching sibling binding is a spec error.
        role = _role(sh=_echo_cmd)
        graph = {
            "inputs": {},
            "nodes": [
                {"id": "cmd", "kind": "tool", "tool": "sh", "args": {"command": {"$fmt": "run {here}"}}},
            ],
            "output": {"$ref": "cmd"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)

    def test_fmt_empty_template_rejected(self, workspace):
        role = _role(sh=_echo_cmd)
        graph = {
            "inputs": {},
            "nodes": [
                {"id": "cmd", "kind": "tool", "tool": "sh", "args": {"command": {"$fmt": "  "}}},
            ],
            "output": {"$ref": "cmd"},
        }
        with pytest.raises(ToolError, match="invalid graph spec"):
            _call(role, graph)
