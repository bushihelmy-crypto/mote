from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mote.orchestration.workflows import END, START, NoOutput, WorkflowBuilder
from mote.orchestration.workflows.definition import WorkflowDefinition
from mote.orchestration.workflows.types import GraphState, Stage


class _State(GraphState):
    value: int = 0


async def _increment(state: _State) -> Stage:
    async def submit():
        return {"value": state.value + 1}

    return Stage(submit=submit())


async def _decrement(state: _State) -> Stage:
    async def submit():
        return {"value": state.value - 1}

    return Stage(submit=submit())


def _builder(fn=_increment, *, recursion_limit: int = 100) -> WorkflowBuilder:
    graph = WorkflowBuilder(
        "identity",
        state_schema=_State,
        output=NoOutput,
        recursion_limit=recursion_limit,
    )
    graph.add_node("step", fn, params={"value": {"from": "$input.value"}})
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph


def test_compiler_is_deterministic_and_content_addressed() -> None:
    first = _builder().build()
    second = _builder().build()
    assert first.definition_id == second.definition_id
    assert first.digest == second.digest
    assert first.canonical_payload == second.canonical_payload
    assert first.definition_id == f"mote.workflow.v1.sha256-{first.digest}"


def test_semantic_definition_changes_advance_identity() -> None:
    baseline = _builder().build().definition_id
    assert _builder(_decrement).build().definition_id != baseline
    assert _builder(recursion_limit=101).build().definition_id != baseline

    changed = _builder()
    changed.add_node(
        "step",
        _increment,
        params={"value": {"from": "$input.value", "description": "changed"}},
    )
    assert changed.build().definition_id != baseline


def test_explicit_implementation_identity_is_part_of_digest() -> None:
    first = _builder()
    first.add_node("step", _increment, params={}, implementation_id="catalog:step/v1")
    second = _builder()
    second.add_node("step", _increment, params={}, implementation_id="catalog:step/v2")
    assert first.build().definition_id != second.build().definition_id


def test_unencoded_closure_fails_closed_until_product_supplies_catalog_identity() -> None:
    opaque = object()

    async def node(state: _State) -> Stage:
        if opaque is None:
            raise AssertionError
        return await _increment(state)

    graph = _builder(node)
    with pytest.raises(ValueError, match="unencoded builtins.object"):
        graph.build()

    graph.add_node("step", node, params={}, implementation_id="product.catalog/node/v1")
    assert graph.build().definition_id.startswith("mote.workflow.v1.sha256-")


def test_definition_envelope_rejects_unknown_version_and_digest_mismatch() -> None:
    definition = _builder().build()
    with pytest.raises(ValueError, match="unknown.*schema version"):
        WorkflowDefinition(
            "mote.workflow-definition/v2",
            definition.definition_id,
            definition.definition_version,
            definition.digest,
            definition.canonical_payload,
            definition._graph,
        )


def test_definition_rejects_digest_substitution() -> None:
    definition = _builder().build()
    with pytest.raises(ValueError, match="digest mismatch"):
        WorkflowDefinition(
            definition.schema_version,
            definition.definition_id,
            definition.definition_version,
            "0" * 64,
            definition.canonical_payload,
            definition._graph,
        )


def test_all_production_entrypoints_delegate_to_canonical_definition() -> None:
    graph_source = Path("orchestration/workflows/graph.py").read_text(encoding="utf-8")
    product_source = Path("product/workflows/agent_service.py").read_text(encoding="utf-8")
    assert "return WorkflowDefinitionCompiler.compile(self)" in graph_source
    assert "return self.build().compile()" in graph_source
    assert "return await self.build().arun(" in graph_source
    assert "definition = graph.build()" in product_source
    assert "WorkflowDefinition(" not in product_source


def test_identity_does_not_use_checkout_path_source_or_object_address() -> None:
    source = Path("orchestration/workflows/definition.py").read_text(encoding="utf-8")
    assert "inspect.getsource" not in source
    assert "co_filename" not in source
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "id"
        for node in ast.walk(tree)
    )
    assert "repr(value)" not in source


def test_product_run_graph_compiles_through_stable_node_catalog_identity() -> None:
    from mote.product.workflows.run_graph.compiler import build_graph
    from mote.product.workflows.run_graph.spec import GraphSpec

    async def dispatch(_name, _arguments):
        raise AssertionError("definition compilation must not execute tools")

    spec = GraphSpec.model_validate(
        {
            "nodes": [
                {
                    "id": "value",
                    "kind": "compute",
                    "expr": "x + 1",
                    "args": {"x": 1},
                }
            ],
            "output": {"$ref": "value"},
        }
    )
    graph = build_graph(spec, dispatch=dispatch, command_name="run_graph")
    definition = graph.build()
    assert "mote.product.run-graph-node.v1.compute.sha256-" in definition.canonical_payload
