#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Import-time layering guard for ``metagpt.executor.compress``.

The compression package is a leaf: it may import ``metagpt.common.*`` and
executor-internal siblings, but must never reach up into the higher layers
(``router`` / ``roles`` / ``context``). This mirrors the import-time layering
enforcement introduced in commit b595d04 — a static AST scan of every module
in the package for forbidden ``import`` / ``from ... import`` statements.
"""
from __future__ import annotations

import ast
from pathlib import Path

import metagpt.executor.compress as compress_pkg

_FORBIDDEN_ROOTS = ("metagpt.router", "metagpt.roles", "metagpt.context")


def _module_names(node: ast.AST) -> list[str]:
    """Fully-qualified module targets of an import statement."""
    names: list[str] = []
    if isinstance(node, ast.Import):
        names.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0 and node.module:
            names.append(node.module)
    return names


def _iter_package_files() -> list[Path]:
    pkg_dir = Path(compress_pkg.__file__).parent
    return sorted(pkg_dir.glob("*.py"))


def test_no_forbidden_imports():
    offenders: list[str] = []
    for py_file in _iter_package_files():
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            for module in _module_names(node):
                if any(module == root or module.startswith(root + ".") for root in _FORBIDDEN_ROOTS):
                    offenders.append(f"{py_file.name}: imports {module}")
    assert not offenders, "compress package must not import router/roles/context:\n" + "\n".join(offenders)


def test_package_has_modules():
    # Guard against a false-pass if the package layout changes.
    files = {p.name for p in _iter_package_files()}
    assert {"base.py", "registry.py", "git.py", "pytest.py", "ruff.py"} <= files
