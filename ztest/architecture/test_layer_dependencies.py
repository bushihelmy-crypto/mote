"""Whole-package import-direction guard.

The small baseline is an explicit migration queue, not a layer-wide exemption:
every entry names one source file and one imported module.  New upward edges
therefore fail immediately, while the two pre-existing seams can be removed
independently as their implementations move to the owning layer.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Layers sharing a rank are peer subsystems at the same architectural depth.
LAYER_RANK = {
    "common": 0,
    "context": 1,
    "executor": 1,
    "router": 1,
    "session": 1,
    "parser": 2,
    "think": 2,
    "loop": 2,
    "roles": 3,
    "environment": 4,
    "cli": 5,
}

# Existing violations frozen at exact import sites. Delete an entry as soon as
# that implementation is moved behind a lower-layer Protocol/module boundary.
MIGRATION_BASELINE: set[tuple[str, str]] = set()


def _is_type_checking_guard(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


class _RuntimeImports(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[tuple[int, str]] = []
        self._type_only = 0

    def visit_If(self, node: ast.If) -> None:
        guarded = _is_type_checking_guard(node.test)
        if guarded:
            self._type_only += 1
        for child in node.body:
            self.visit(child)
        if guarded:
            self._type_only -= 1
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:
        if not self._type_only:
            self.imports.extend((node.lineno, alias.name) for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._type_only and node.module:
            self.imports.append((node.lineno, node.module))


def _top_level(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "mote":
        return parts[1]
    return None


def test_runtime_imports_follow_layer_direction() -> None:
    violations: list[str] = []
    seen_baseline: set[tuple[str, str]] = set()

    for source_layer, source_rank in LAYER_RANK.items():
        for path in (PACKAGE_ROOT / source_layer).rglob("*.py"):
            visitor = _RuntimeImports()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            for lineno, module in visitor.imports:
                target_layer = _top_level(module)
                if target_layer not in LAYER_RANK or LAYER_RANK[target_layer] <= source_rank:
                    continue
                edge = (relative, module)
                if edge in MIGRATION_BASELINE:
                    seen_baseline.add(edge)
                    continue
                violations.append(f"{relative}:{lineno}: {source_layer} -> {module}")

    stale = MIGRATION_BASELINE - seen_baseline
    assert not stale, "Remove resolved entries from MIGRATION_BASELINE:\n" + "\n".join(sorted(map(str, stale)))
    assert not violations, "Upward runtime imports are forbidden:\n" + "\n".join(violations)
