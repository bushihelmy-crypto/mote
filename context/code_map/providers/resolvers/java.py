"""Java package ⇄ file arithmetic — ``package``-anchored source-root mapping.

Java's import is a fully-qualified type name (``com.example.util.Helper``), and
the language guarantees a name→path law: a type ``a.b.C`` lives at ``a/b/C.java``
under a *source root*. This resolver recovers the source root from a file's own
``package`` declaration — a file at ``…/src/main/java/com/example/App.java`` whose
package is ``com.example`` means the root is ``…/src/main/java`` (strip the package
dirs off the file's directory). :meth:`module_to_path` then maps an imported
``com.example.util.Helper`` to ``<root>/com/example/util/Helper.java`` (probing
each known root), and — because an import may name an inner/static member of a
type (``com.example.util.Const.MAX``) — also probes the name minus its last
segment. A JDK / third-party import (``java.util.List``) matches no repo root →
``None`` (dropped, not guessed).
"""

from __future__ import annotations

import os
import re
from typing import Optional

_PACKAGE_RE = re.compile(rb"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def _package_of(abspath: str) -> Optional[str]:
    """The dotted package a ``.java`` file declares, or ``None`` (default pkg)."""
    try:
        with open(abspath, "rb") as f:
            head = f.read(8192)  # the package line is always at the top
    except OSError:
        return None
    match = _PACKAGE_RE.search(head)
    return match.group(1).decode("utf-8", "replace") if match else None


def _source_root(abspath: str) -> Optional[str]:
    """The dir under which ``package a.b`` ⇒ ``a/b/`` — the file's dir minus pkg.

    A file ``…/java/com/example/App.java`` with ``package com.example`` roots at
    ``…/java``. Default package (no declaration) roots at the file's own directory.
    """
    directory = os.path.dirname(abspath)
    package = _package_of(abspath)
    if not package:
        return directory
    parts = package.split(".")
    root = directory
    for _ in parts:
        parent = os.path.dirname(root)
        if parent == root:
            return None  # package deeper than the path — give up
        root = parent
    # The stripped tail must actually equal the package dirs (sanity anchor).
    rel = os.path.relpath(directory, root).replace(os.sep, ".")
    return root if rel == package else directory


class JavaModuleResolver:
    """Java ``a.b.C`` ⇄ ``a/b/C.java`` resolution, anchored on ``package`` lines."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """The source roots recovered from the touched Java files' packages."""
        roots: set[str] = set()
        for f in abs_files:
            if not f.endswith(".java"):
                continue
            root = _source_root(f)
            if root is not None:
                roots.add(root)
        return roots

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """A fully-qualified type ``a.b.C`` → ``<root>/a/b/C.java``, else ``None``.

        Probes each source root; also probes the name minus its last segment so an
        imported inner/static member (``a.b.C.MEMBER``) resolves to ``a/b/C.java``.
        """
        if not module:
            return None
        segments = [s for s in module.split(".") if s]
        for root in roots:
            candidate = os.path.join(root, *segments) + ".java"
            if os.path.exists(candidate):
                return candidate
            if len(segments) > 1:
                outer = os.path.join(root, *segments[:-1]) + ".java"
                if os.path.exists(outer):
                    return outer
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        """The fully-qualified type name(s) *abspath* is importable by."""
        if not abspath.endswith(".java"):
            return set()
        root = _source_root(abspath)
        if root is None:
            return set()
        rel = os.path.relpath(abspath, root)
        if rel.startswith(".."):
            return set()
        stem = rel[:-5] if rel.endswith(".java") else rel  # drop ".java"
        return {stem.replace(os.sep, ".")}

    def is_relative(self, module: str) -> bool:
        """Java has no relative imports — every import is a fully-qualified name."""
        return False


__all__ = ["JavaModuleResolver"]
