"""Runtime graph constructors must describe state, never activate resources."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from types import ModuleType
from typing import Iterator

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_MANIFESTS = (
    *sorted((_PACKAGE_ROOT / "roles/runtime_modules").glob("*.py")),
    _PACKAGE_ROOT / "roles/role_components.py",
)
_ACTIVATION_CALLS = {
    "connect",
    "create_task",
    "discover",
    "makedirs",
    "mkdir",
    "open",
    "Popen",
    "run",
    "start",
    "write_bytes",
    "write_text",
}


def _call_name(call: ast.Call) -> str | None:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _imports(tree: ast.AST) -> dict[str, tuple[str, str]]:
    imported: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            imported[alias.asname or alias.name] = (node.module, alias.name)
    return imported


def _classes_constructed_by_manifests() -> Iterator[type]:
    seen: set[type] = set()
    for manifest in _MANIFESTS:
        tree = ast.parse(manifest.read_text(encoding="utf-8"), filename=str(manifest))
        imported = _imports(tree)
        module: ModuleType = importlib.import_module(
            "mote." + manifest.relative_to(_PACKAGE_ROOT).with_suffix("").as_posix().replace("/", ".")
        )
        local_classes = {node.name: getattr(module, node.name) for node in tree.body if isinstance(node, ast.ClassDef)}
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = _call_name(call)
            candidate = local_classes.get(name)
            if candidate is None and name in imported:
                module_name, symbol = imported[name]
                candidate = getattr(importlib.import_module(module_name), symbol, None)
            if inspect.isclass(candidate) and candidate not in seen:
                seen.add(candidate)
                yield candidate


def test_runtime_component_constructors_do_not_activate_resources():
    violations: list[str] = []
    discovered = list(_classes_constructed_by_manifests())
    for runtime_class in discovered:
        path_string = inspect.getsourcefile(runtime_class)
        if path_string is None:
            continue
        path = Path(path_string)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != runtime_class.__name__:
                continue
            constructor = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__"
                ),
                None,
            )
            if constructor is None:
                continue
            for call in ast.walk(constructor):
                if not isinstance(call, ast.Call):
                    continue
                name = _call_name(call)
                if name in _ACTIVATION_CALLS:
                    violations.append(
                        f"{path.relative_to(_PACKAGE_ROOT)}:{call.lineno} {node.name}.__init__ calls {name}()"
                    )
    names = {runtime_class.__name__ for runtime_class in discovered}
    assert {"ToolExecutor", "RepoIndexer", "SessionLog"} <= names
    assert not violations, "constructor activation is forbidden:\n" + "\n".join(violations)
