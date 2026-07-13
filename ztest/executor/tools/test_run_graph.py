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
        role = self._role_with_graph_tool("MediaPipeline")
        graph = {
            "inputs": {"xs": {"type": "list", "description": "xs"}},
            "nodes": [
                {
                    "id": "m",
                    "kind": "map",
                    "tool": "MediaPipeline",
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
