"""Executable Tool binding remains a single compiled, generation-bound path."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_catalog_and_executor_have_one_compiled_binding_authority() -> None:
    binding = _source("runtime/tools/tool_binding.py")
    catalog = _source("runtime/tools/tool_catalog.py")
    executor = _source("runtime/tools/tool_executor.py")
    executor_tree = ast.parse(executor)

    assert "wrapped_tool" not in binding
    assert "_live_tools" not in catalog
    assert not any(isinstance(node, ast.Attribute) and node.attr == "_tools" for node in ast.walk(executor_tree))
    assert "dict[str, ExecutableToolBinding]" in catalog
    assert "run_pinned_command" in executor


def test_snapshot_pins_binding_and_both_authoritative_generations() -> None:
    contract = _source("contracts/tool/catalog.py")
    registry = _source("runtime/tools/bound_registry.py")
    snapshots = _source("runtime/tools/snapshots.py")

    assert "composition_generation_id: str" in contract
    assert "catalog_generation: int" in registry
    assert "binding: ExecutableToolBinding" in registry
    assert "Callable" not in registry
    assert "run_pinned_command" in snapshots
    assert ".run_command(" not in snapshots


def test_graph_output_contract_has_one_contracts_owner_and_output_engine_path() -> None:
    graph_spec = _source("product/workflows/run_graph/spec.py")
    graph_service = _source("runtime/output/graph_service.py")

    assert "class GraphOutputContractSpec" not in graph_spec
    assert "from mote.contracts.output.graph import GraphOutputContractSpec" in graph_spec
    assert "OutputEngine" in graph_service
    assert "FinalCandidateAction" in graph_service
    assert "ToolResult" not in graph_service
    assert not (ROOT / "runtime/output/graph_committer.py").exists()


def test_tool_binding_modules_have_no_local_import_escape_hatches() -> None:
    for relative in (
        "runtime/tools/tool_binding.py",
        "runtime/tools/tool_catalog.py",
        "runtime/tools/tool_executor.py",
        "runtime/tools/bound_registry.py",
        "runtime/tools/snapshots.py",
    ):
        tree = ast.parse(_source(relative), filename=relative)
        assert not any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            for top_level in tree.body
            if isinstance(top_level, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in ast.walk(top_level)
        ), relative
