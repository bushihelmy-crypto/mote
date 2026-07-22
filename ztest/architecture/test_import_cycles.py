"""Runtime import cycles are frozen at exact strongly connected components."""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_BASELINE: set[tuple[str, ...]] = set()


def _is_type_checking(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


class _RuntimeEdges(ast.NodeVisitor):
    def __init__(self, source: str, modules: set[str], graph: dict[str, set[str]]) -> None:
        self.source = source
        self.modules = modules
        self.graph = graph
        self.type_only = 0

    def visit_If(self, node: ast.If) -> None:
        guarded = _is_type_checking(node.test)
        self.type_only += guarded
        for child in node.body:
            self.visit(child)
        self.type_only -= guarded
        for child in node.orelse:
            self.visit(child)

    def _add(self, module: str) -> None:
        if self.type_only or not module.startswith("mote."):
            return
        target = module.removeprefix("mote.")
        if target in self.modules:
            self.graph[self.source].add(target)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._add(node.module)


def _runtime_graph() -> tuple[set[str], dict[str, set[str]]]:
    paths = [path for path in PACKAGE_ROOT.rglob("*.py") if "ztest" not in path.parts and ".venv" not in path.parts]
    modules = {path.relative_to(PACKAGE_ROOT).with_suffix("").as_posix().replace("/", ".") for path in paths}
    graph: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        source = path.relative_to(PACKAGE_ROOT).with_suffix("").as_posix().replace("/", ".")
        visitor = _RuntimeEdges(source, modules, graph)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return modules, graph


def _strong_components(modules: set[str], graph: dict[str, set[str]]) -> set[tuple[str, ...]]:
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    components: set[tuple[str, ...]] = set()

    def visit(module: str) -> None:
        indexes[module] = lowlinks[module] = len(indexes)
        stack.append(module)
        stacked.add(module)
        for target in graph[module]:
            if target not in indexes:
                visit(target)
                lowlinks[module] = min(lowlinks[module], lowlinks[target])
            elif target in stacked:
                lowlinks[module] = min(lowlinks[module], indexes[target])
        if lowlinks[module] != indexes[module]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            stacked.remove(target)
            component.append(target)
            if target == module:
                break
        if len(component) > 1:
            components.add(tuple(sorted(component)))

    for module in modules:
        if module not in indexes:
            visit(module)
    return components


def test_runtime_import_cycles_only_shrink() -> None:
    current = _strong_components(*_runtime_graph())
    stale = MIGRATION_BASELINE - current
    added = current - MIGRATION_BASELINE
    assert not stale, "Delete resolved cycles from MIGRATION_BASELINE:\n" + "\n".join(map(str, sorted(stale)))
    assert not added, "New runtime import cycles are forbidden:\n" + "\n".join(map(str, sorted(added)))
