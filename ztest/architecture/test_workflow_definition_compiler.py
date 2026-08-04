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


def _builder(
    fn=_increment,
    *,
    recursion_limit: int = 100,
    implementation_id: str = "test.catalog.increment/v1",
) -> WorkflowBuilder:
    graph = WorkflowBuilder(
        "identity",
        state_schema=_State,
        output=NoOutput,
        recursion_limit=recursion_limit,
    )
    graph.add_node(
        "step",
        fn,
        params={"value": {"from": "$input.value"}},
        implementation_id=implementation_id,
    )
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph


def test_compiler_is_deterministic_and_content_addressed() -> None:
    first = _builder().build()
    second = _builder().build()
    assert first.definition.definition_id == second.definition.definition_id
    assert first.definition.digest == second.definition.digest
    assert first.definition.canonical_payload == second.definition.canonical_payload
    assert first.definition.definition_id == f"mote.workflow.v1.sha256-{first.definition.digest}"


def test_semantic_definition_changes_advance_identity() -> None:
    baseline = _builder().build().definition.definition_id
    assert _builder(_decrement).build().definition.definition_id == baseline
    assert (
        _builder(_decrement, implementation_id="test.catalog.decrement/v1").build().definition.definition_id != baseline
    )
    assert _builder(recursion_limit=101).build().definition.definition_id != baseline

    changed = _builder()
    changed.add_node(
        "step",
        _increment,
        params={"value": {"from": "$input.value", "description": "changed"}},
        implementation_id="test.catalog.increment/v1",
    )
    assert changed.build().definition.definition_id != baseline


def test_explicit_implementation_identity_is_part_of_digest() -> None:
    first = _builder()
    first.add_node("step", _increment, params={}, implementation_id="catalog:step/v1")
    second = _builder()
    second.add_node("step", _increment, params={}, implementation_id="catalog:step/v2")
    assert first.build().definition.definition_id != second.build().definition.definition_id


def test_callable_requires_product_catalog_identity() -> None:
    opaque = object()

    async def node(state: _State) -> Stage:
        if opaque is None:
            raise AssertionError
        return await _increment(state)

    graph = WorkflowBuilder("opaque", state_schema=_State, output=NoOutput)
    graph.add_node("step", node)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    with pytest.raises(ValueError, match="explicit versioned implementation identity"):
        graph.build()

    graph.add_node("step", node, params={}, implementation_id="product.catalog/node/v1")
    assert graph.build().definition.definition_id.startswith("mote.workflow.v1.sha256-")


def test_definition_envelope_rejects_unknown_version_and_digest_mismatch() -> None:
    definition = _builder().build().definition
    with pytest.raises(ValueError, match="unknown.*schema version"):
        WorkflowDefinition(
            "mote.workflow-definition/v2",
            definition.definition_id,
            definition.definition_version,
            definition.digest,
            definition.canonical_payload,
        )


def test_definition_rejects_digest_substitution() -> None:
    definition = _builder().build().definition
    with pytest.raises(ValueError, match="digest mismatch"):
        WorkflowDefinition(
            definition.schema_version,
            definition.definition_id,
            definition.definition_version,
            "0" * 64,
            definition.canonical_payload,
        )


def test_all_production_entrypoints_delegate_to_canonical_definition() -> None:
    graph_source = Path("orchestration/workflows/graph.py").read_text(encoding="utf-8")
    product_source = Path("product/workflows/agent_service.py").read_text(encoding="utf-8")
    assert "return WorkflowDefinitionCompiler.compile(self)" in graph_source
    assert "return self.build().compile()" in graph_source
    assert "return await self.build().arun(" in graph_source
    assert "resolve_definition_source(" in product_source
    assert "expected_definition_id=live_definition.definition_id" in product_source
    assert "Workflow resume cannot accept a live graph continuation" in product_source
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
    executable = graph.build()
    assert "mote.product.run-graph-node.v1.compute.sha256-" in executable.definition.canonical_payload
