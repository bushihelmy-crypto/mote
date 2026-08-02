"""R3.1 gates for authoritative error imports without a Runtime facade."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _python_files() -> list[Path]:
    files: list[Path] = []
    for package in ("contracts", "kernel", "runtime", "orchestration", "product", "ztest"):
        files.extend((ROOT / package).rglob("*.py"))
    return sorted(files)


def test_runtime_error_aggregation_facade_is_deleted() -> None:
    assert not (ROOT / "runtime" / "errors" / "__init__.py").exists()
    assert list((ROOT / "runtime" / "errors").glob("*.py")) == []


def test_no_consumer_imports_runtime_error_aggregation_facade() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mote.runtime.errors"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node, ast.Import) and any(
                alias.name.startswith("mote.runtime.errors") for alias in node.names
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_cross_boundary_errors_remain_contracts_owned() -> None:
    from mote.contracts.agent.errors import AgentLimitReached
    from mote.contracts.foundation.errors.report import ErrorReport
    from mote.contracts.output.errors import OutputCommitFencedError
    from mote.contracts.task.graph_errors import GraphError
    from mote.contracts.tool.errors import ToolError

    assert AgentLimitReached.__module__ == "mote.contracts.agent.errors"
    assert ErrorReport.__module__ == "mote.contracts.foundation.errors.report"
    assert OutputCommitFencedError.__module__ == "mote.contracts.output.errors"
    assert GraphError.__module__ == "mote.contracts.task.graph_errors"
    assert ToolError.__module__ == "mote.contracts.tool.errors"
