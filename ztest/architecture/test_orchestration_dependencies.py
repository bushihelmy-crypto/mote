"""Executable ownership gates for the four Orchestration capabilities."""

from __future__ import annotations

import ast
import importlib.util
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = {"agents", "automation", "background_tasks", "workflows"}


def _imports(path: Path) -> list[tuple[int, str]]:
    package = ".".join(("mote", *path.relative_to(PACKAGE_ROOT).parts[:-1]))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            found.append((node.lineno, module))
    return found


def _capability(module: str) -> str | None:
    prefix = "mote.orchestration."
    if not module.startswith(prefix):
        return None
    candidate = module.removeprefix(prefix).split(".", 1)[0]
    return candidate if candidate in CAPABILITIES else None


def _scc(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    active: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in graph.get(node, ()):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                active.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components)


def test_orchestration_capabilities_have_no_direct_imports() -> None:
    violations: list[str] = []
    for source in sorted(CAPABILITIES):
        for path in (PACKAGE_ROOT / "orchestration" / source).rglob("*.py"):
            for lineno, module in _imports(path):
                target = _capability(module)
                if target is not None and target != source:
                    relative = path.relative_to(PACKAGE_ROOT)
                    violations.append(f"{relative}:{lineno}: {source} -> {target}")
    assert not violations, "Cross-capability imports are forbidden:\n" + "\n".join(violations)


def test_orchestration_secondary_packages_are_acyclic() -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for capability in sorted(CAPABILITIES):
        root = PACKAGE_ROOT / "orchestration" / capability
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            source = f"{capability}.{relative.parts[0]}" if len(relative.parts) > 1 else capability
            graph.setdefault(source, set())
            for _, module in _imports(path):
                target_capability = _capability(module)
                if target_capability != capability:
                    continue
                parts = module.split(".")
                target = f"{capability}.{parts[3]}" if len(parts) > 3 else capability
                if target != source:
                    graph[source].add(target)
    assert not _scc(graph), f"Orchestration package cycles: {_scc(graph)}"


def test_legacy_orchestration_packages_are_deleted() -> None:
    assert not (PACKAGE_ROOT / "orchestration/environment").exists()
    assert not (PACKAGE_ROOT / "orchestration/tasks").exists()


def test_agents_do_not_probe_role_private_wiring() -> None:
    forbidden = {"_config", "_context", "_capabilities"}
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "orchestration/agents").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: {node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in forbidden
            ):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: getattr")
    assert not violations, "Agent control probes Runtime role internals:\n" + "\n".join(violations)


def test_background_task_model_has_no_workflow_state() -> None:
    path = PACKAGE_ROOT / "orchestration/background_tasks/model.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = {
        "graph_meta",
        "run_state",
        "state_snapshot",
        "completed_nodes",
        "retry_count",
        "max_restarts",
    }
    task_meta = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskMeta")
    fields = {
        target.id
        for node in task_meta.body
        if isinstance(node, ast.AnnAssign) and isinstance((target := node.target), ast.Name)
    }
    assert not fields & forbidden


def test_background_pool_does_not_import_workflow_control() -> None:
    path = PACKAGE_ROOT / "orchestration/background_tasks/pool.py"
    imports = {module for _, module in _imports(path)}
    assert "mote.contracts.workflow_control" not in imports
    assert not any(module.startswith("mote.orchestration.workflows") for module in imports)


def test_tool_execution_classification_uses_definition_kind_only() -> None:
    roots = [PACKAGE_ROOT / name for name in ("kernel", "runtime", "product", "orchestration")]
    forbidden = {"_is_bg_pipeline_executor", "is_graph_tool"}
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden:
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: {node.attr}")
    assert not violations, "Legacy workflow classification remains:\n" + "\n".join(violations)
