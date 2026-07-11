#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Import-time layering guard for ``mote.common.text``.

``common`` is the bottom layer — the only one every other layer may import. This
subpackage must therefore never reach *up* into any higher layer, and its core
``elision.py`` must be pure stdlib (no ``mote.`` import at all). A static AST scan
mirrors ``ztest/executor/compress/test_layering.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import mote.common.text as text_pkg

# Every layer that sits above ``common``. ``common.text`` must import none of them.
_FORBIDDEN_ROOTS = (
    "mote.router",
    "mote.roles",
    "mote.context",
    "mote.executor",
    "mote.environment",
    "mote.session",
    "mote.loop",
    "mote.cli",
    "mote.memory",
    "mote.parser",
    "mote.sandbox",
    "mote.think",
)


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
    pkg_dir = Path(text_pkg.__file__).parent
    return sorted(pkg_dir.glob("*.py"))


def test_no_forbidden_imports():
    offenders: list[str] = []
    for py_file in _iter_package_files():
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            for module in _module_names(node):
                if any(module == root or module.startswith(root + ".") for root in _FORBIDDEN_ROOTS):
                    offenders.append(f"{py_file.name}: imports {module}")
    assert not offenders, "common.text must not import any higher layer:\n" + "\n".join(offenders)


def _mote_imports(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found: list[str] = []
    for node in ast.walk(tree):
        for module in _module_names(node):
            if module == "mote" or module.startswith("mote."):
                found.append(module)
    return found


def test_core_modules_are_pure_stdlib():
    """The core value/marker modules must not import any ``mote.*`` (pure stdlib).

    Only ``__init__.py`` is allowed to re-export from sibling ``mote.common.text``
    submodules; the leaf modules stay dependency-free.
    """
    pkg_dir = Path(text_pkg.__file__).parent
    offenders: list[str] = []
    for name in (
        "elision.py",
        "markers.py",
        "ansi.py",
        "plural.py",
        "paths.py",
        "humanize.py",
        "whitespace.py",
        "hashing.py",
    ):
        for module in _mote_imports(pkg_dir / name):
            offenders.append(f"{name}: imports {module}")
    assert not offenders, "core common.text modules must be pure stdlib:\n" + "\n".join(offenders)


def test_package_has_modules():
    files = {p.name for p in _iter_package_files()}
    assert {
        "__init__.py",
        "elision.py",
        "markers.py",
        "ansi.py",
        "plural.py",
        "paths.py",
        "humanize.py",
        "whitespace.py",
        "hashing.py",
    } <= files
