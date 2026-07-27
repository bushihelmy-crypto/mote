"""Python's module ⇄ file arithmetic — the one :class:`ModuleResolver` for ``.py``.

This is the *stateless* half of the Python language seam: pure name↔path math,
no ``ast`` and no I/O beyond ``os.path.exists`` probes. Every method here is the
verbatim logic that used to live as private statics on the CodeMap facade
(:mod:`mote.runtime.context.code_map` — ``_import_roots`` / ``_module_to_path`` /
``_module_candidates``) and the extractor (``_package_segments`` /
``_resolve_relative``), lifted behind the :class:`~mote.runtime.context.code_map.providers.base.ModuleResolver`
protocol so the facade can group a touched set *by resolver* and never try to
resolve a ``.go`` import with Python's ``a.b.c -> a/b/c`` rule.

Two extraction-time helpers (``package_segments`` / ``resolve_relative``) are
public because the Python *provider* needs them to rewrite a relative import
(``from . import x``) to the absolute dotted target it reaches; the four
protocol methods are what the *facade* consumes for cross-file edges.
"""

from __future__ import annotations

import os
from typing import Optional

_INIT = "__init__.py"


class PythonModuleResolver:
    """Python ``a.b.c`` ⇄ ``a/b/c.py`` name arithmetic (package-aware, LSP-free)."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """Directory anchors under which an absolute dotted import maps to a file.

        A touched file at ``.../repo/pkg/sub/mod.py`` imported as ``pkg.sub.other``
        tells us ``.../repo`` is an import root: walking up past every package
        directory (those with an ``__init__.py``) lands on the path prefix that
        ``a.b.c`` -> ``a/b/c`` is relative to. We infer these purely from the
        touched set — no cwd, no ``sys.path`` — so mapping stays locality-driven.
        Non-package files (a bare script) anchor at their own directory.
        """
        roots: set[str] = set()
        for f in abs_files:
            if not f.endswith(".py"):
                continue
            d = os.path.dirname(f)
            # Climb out of the package chain: while the dir is itself a package,
            # its parent is the more-rooted anchor for a top-level dotted name.
            while os.path.exists(os.path.join(d, _INIT)):
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
            roots.add(d)
        return roots

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """Map a dotted ``module`` to a repo file under one of ``roots`` (or None).

        ``a.b.c`` -> ``<root>/a/b/c.py`` or ``<root>/a/b/c/__init__.py``, taking the
        first root that resolves. Relative (leading-dot) or unanchored modules —
        and any that map to no file on disk (stdlib / third-party) — return None.
        """
        if not module or module.startswith("."):
            return None
        rel = module.replace(".", os.sep)
        for root in roots:
            mod = os.path.join(root, rel + ".py")
            if os.path.exists(mod):
                return mod
            pkg = os.path.join(root, rel, _INIT)
            if os.path.exists(pkg):
                return pkg
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        """Dotted module names *abspath* could be imported by (locality heuristic).

        ``.../a/b/c.py`` -> {``c``, ``b.c``, ``a.b.c``}. A package ``__init__.py``
        also offers its parent-dir name (``.../pkg/__init__.py`` -> ``pkg`` ...).
        Not a resolver — just the plausible import spellings to string-match.
        """
        if not abspath.endswith(".py"):
            return set()
        directory, filename = os.path.split(abspath)
        stem = filename[:-3]  # drop ".py"
        parts = [p for p in directory.split(os.sep) if p]
        if stem == "__init__":
            # A package's importable name is its directory chain, not "__init__".
            segments = parts
        else:
            segments = parts + [stem]
        if not segments:
            return set()
        # Progressive right-anchored dotted suffixes: c, b.c, a.b.c ...
        cands: set[str] = set()
        for i in range(len(segments)):
            cands.add(".".join(segments[i:]))
        return cands

    def is_relative(self, module: str) -> bool:
        """True when *module* is a relative (leading-dot) import spelling."""
        return module.startswith(".")

    # -- extraction-time helpers (consumed by the Python provider) -----------

    @staticmethod
    def package_segments(abspath: str) -> Optional[list[str]]:
        """The importing file's package chain, inferred from adjacent ``__init__``."""
        if not abspath.endswith(".py"):
            return None
        directory = os.path.dirname(abspath)
        segments: list[str] = []
        d = directory
        while os.path.exists(os.path.join(d, _INIT)):
            segments.append(os.path.basename(d))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        segments.reverse()  # outermost package first
        return segments

    @staticmethod
    def resolve_relative(pkg: Optional[list[str]], level: int, module: Optional[str]) -> Optional[str]:
        """Absolute dotted name a relative import reaches, or None if unanchorable."""
        if not pkg:
            return None
        climb = level - 1
        if climb > len(pkg):
            return None
        base = pkg[: len(pkg) - climb]
        tail = module.split(".") if module else []
        segments = base + tail
        return ".".join(segments)


__all__ = ["PythonModuleResolver"]
