"""AST import scanner shared by architecture tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ImportEdge:
    source_module: str
    target_module: str
    line: int
    kind: str
    type_checking: bool
    dynamic: bool


class _Scanner(ast.NodeVisitor):
    def __init__(self, source_module: str, package: str) -> None:
        self.source_module = source_module
        self.package = package
        self.edges: list[ImportEdge] = []
        self._type_checking = 0

    def visit_If(self, node: ast.If) -> None:
        is_type_checking = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        self._type_checking += int(is_type_checking)
        for child in node.body:
            self.visit(child)
        self._type_checking -= int(is_type_checking)
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node.lineno, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = node.module or ""
        if node.level:
            parts = self.package.split(".")
            prefix = parts[: max(0, len(parts) - node.level + 1)]
            target = ".".join((*prefix, target)) if target else ".".join(prefix)
        self._add(target, node.lineno, "from")

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            name = f"{node.func.value.id}.{node.func.attr}"
        if name in {"__import__", "importlib.import_module"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self.edges.append(
                    ImportEdge(self.source_module, first.value, node.lineno, "dynamic", bool(self._type_checking), True)
                )
        self.generic_visit(node)

    def _add(self, target: str, line: int, kind: str) -> None:
        if target:
            self.edges.append(ImportEdge(self.source_module, target, line, kind, bool(self._type_checking), False))


def module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("mote", *parts))


def scan_file(path: Path, root: Path) -> tuple[ImportEdge, ...]:
    source = module_name(path, root)
    package = source if path.name == "__init__.py" else source.rpartition(".")[0]
    scanner = _Scanner(source, package)
    scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return tuple(scanner.edges)


def scan_tree(root: Path, package: str = "kernel") -> tuple[ImportEdge, ...]:
    edges: list[ImportEdge] = []
    for path in sorted((root / package).rglob("*.py")):
        edges.extend(scan_file(path, root))
    return tuple(edges)


__all__ = ["ImportEdge", "module_name", "scan_file", "scan_tree"]
