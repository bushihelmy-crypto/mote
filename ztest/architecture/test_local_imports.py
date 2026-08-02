"""Production imports are module-level architecture declarations."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class _NestedImportCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.nesting_depth = 0
        self.violations: list[str] = []

    def _visit_nested(self, node: ast.AST) -> None:
        self.nesting_depth += 1
        self.generic_visit(node)
        self.nesting_depth -= 1

    visit_FunctionDef = _visit_nested
    visit_AsyncFunctionDef = _visit_nested
    visit_ClassDef = _visit_nested

    def visit_Import(self, node: ast.Import) -> None:
        if self.nesting_depth:
            modules = ", ".join(alias.name for alias in node.names)
            self.violations.append(f"{self.path}:{node.lineno}: import {modules}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.nesting_depth:
            module = "." * node.level + (node.module or "")
            self.violations.append(f"{self.path}:{node.lineno}: from {module} import ...")


def test_all_production_imports_are_module_level() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "ztest" in path.parts or ".venv" in path.parts:
            continue
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        collector = _NestedImportCollector(relative)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        violations.extend(collector.violations)

    assert not violations, (
        "All non-ztest imports must be declared at module scope. Resolve dependency "
        "cycles through layering, module extraction, or contracts/ports Protocols; "
        "do not hide them behind local imports:\n" + "\n".join(sorted(violations))
    )


def test_nested_import_gate_rejects_a_negative_fixture() -> None:
    tree = ast.parse("""
def hidden_dependency():
    import optional_backend
    from package import adapter
""")
    collector = _NestedImportCollector("negative_fixture.py")
    collector.visit(tree)
    assert collector.violations == [
        "negative_fixture.py:3: import optional_backend",
        "negative_fixture.py:4: from package import ...",
    ]
