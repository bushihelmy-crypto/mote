from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

from mote.kernel.execution.engine import ExecutionEngine
from mote.kernel.execution.graph import nodes
from mote.kernel.execution.operations.container import GraphAssemblyInputs


def test_graph_assembly_fields_resolve_without_unbounded_any() -> None:
    hints = get_type_hints(GraphAssemblyInputs)

    assert set(hints) == {
        "context",
        "observation",
        "inference",
        "actions",
        "outputs",
        "context_provider",
        "completion_policy",
        "current_channel",
        "inference_engine",
        "set_active",
        "inbox_activity",
        "get_bg_pool",
        "advance_turn",
    }
    assert all("Any" not in str(annotation) for annotation in hints.values())


def test_engine_and_builtin_node_public_signatures_do_not_erase_types() -> None:
    constructors = [
        ExecutionEngine.__init__,
        nodes.RestoreNode.__init__,
        nodes.ObserveNode.__init__,
        nodes.BudgetNode.__init__,
        nodes.InferenceNode.__init__,
        nodes.ActNode.__init__,
        nodes.ValidateOutputNode.__init__,
        nodes.AwaitQuiescenceNode.__init__,
    ]

    for constructor in constructors:
        signature = inspect.signature(constructor)
        annotations = [parameter.annotation for parameter in signature.parameters.values() if parameter.name != "self"]
        assert annotations
        assert all(annotation is not inspect.Parameter.empty for annotation in annotations)
        assert all("Any" not in str(annotation) for annotation in annotations)


def test_kernel_graph_type_slice_has_no_any_import_or_annotation() -> None:
    paths = (
        Path("kernel/execution/operations/container.py"),
        Path("kernel/execution/operations/inference.py"),
        Path("kernel/execution/operations/output.py"),
        Path("kernel/execution/graph/nodes.py"),
        Path("kernel/execution/engine.py"),
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import Any" not in source
        assert ": Any" not in source
        assert "-> Any" not in source
        assert inspect.cleandoc(source)
