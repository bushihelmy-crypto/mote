from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
FILEOPS_ROOT = PACKAGE_ROOT / "runtime" / "fileops"


def test_reservation_store_has_one_production_owner():
    constructors: list[str] = []
    for path in FILEOPS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "ProjectRecoveryFenceStore":
                constructors.append(path.name)

    assert constructors == ["control.py"]


def test_removed_file_operation_gates_cannot_return():
    forbidden_functions = {
        "_assert_path_available",
        "assert_clear",
        "fenced_records",
        "reconcile_project",
    }
    violations: list[str] = []
    for path in FILEOPS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in forbidden_functions:
                    violations.append(f"{path.name}:{node.lineno}:{node.name}")

    assert violations == []
