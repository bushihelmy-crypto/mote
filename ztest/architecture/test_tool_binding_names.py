"""R3.3 gates for unambiguous executable and pinned tool bindings."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_tool_binding_names_express_distinct_lifecycles() -> None:
    live = ROOT / "runtime" / "tools" / "tool_binding.py"
    pinned = ROOT / "runtime" / "tools" / "bound_registry.py"
    live_classes = {node.name for node in ast.walk(ast.parse(live.read_text())) if isinstance(node, ast.ClassDef)}
    pinned_classes = {node.name for node in ast.walk(ast.parse(pinned.read_text())) if isinstance(node, ast.ClassDef)}
    assert "ExecutableToolBinding" in live_classes
    assert "PinnedToolInvocation" in pinned_classes
    assert "BoundTool" not in live_classes | pinned_classes


def test_old_bound_tool_symbol_has_no_import_or_alias() -> None:
    violations: list[str] = []
    for package in ("contracts", "kernel", "runtime", "orchestration", "product"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "BoundTool":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if isinstance(node, ast.ImportFrom) and any(alias.name == "BoundTool" for alias in node.names):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if any(isinstance(target, ast.Name) and target.id == "BoundTool" for target in targets):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_snapshot_invocation_executes_retained_binding_not_live_name_lookup() -> None:
    source = (ROOT / "runtime/tools/snapshots.py").read_text(encoding="utf-8")
    dispatch = source[source.index("    async def dispatch") : source.index("    def release")]
    assert "run_pinned_command(" in dispatch
    assert "run_command(" not in dispatch
    assert "_catalog.get(" not in dispatch
