"""Go module ⇄ file arithmetic — import-path ⇄ package-directory mapping.

Go imports are absolute module *paths* (``example.com/repo/pkg/sub``), not files:
a package is a directory of ``.go`` files, and the path's prefix is the module
declared in the repo's ``go.mod``. So this resolver anchors on ``go.mod``:
:meth:`import_roots` finds each ``go.mod`` directory (walking up from the touched
files), :meth:`module_to_path` reads that ``go.mod``'s ``module`` line to turn a
repo-internal import path into the package directory and returns a representative
source file, and :meth:`module_candidates` turns a file back into the one import
path its package is reachable by. A path whose prefix is not the repo module
(``fmt``, ``github.com/x/y``) resolves to nothing — it is an external dependency,
and a wrong edge is worse than a missing one.

TRADE (documented): only bare-name ``Foo()`` calls resolve to same-file edges; a
receiver call ``r.Method()`` needs type information Go's tree alone does not carry,
so no edge is drawn (the same limitation the Python provider has for non-``self``
receivers).
"""

from __future__ import annotations

import os
from typing import Optional

_GO_MOD = "go.mod"


def _find_go_mod_dir(start_dir: str) -> Optional[str]:
    """Nearest ancestor directory (incl. *start_dir*) that holds a ``go.mod``."""
    d = start_dir
    while True:
        if os.path.exists(os.path.join(d, _GO_MOD)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _module_prefix(gomod_dir: str) -> Optional[str]:
    """The module path declared by ``<gomod_dir>/go.mod`` (its ``module`` line)."""
    try:
        with open(os.path.join(gomod_dir, _GO_MOD), encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("module "):
                    return line[len("module ") :].strip()
    except OSError:
        return None
    return None


def _first_go_file(pkg_dir: str) -> Optional[str]:
    """A representative (non-test) ``.go`` file in *pkg_dir*, deterministically."""
    try:
        names = sorted(n for n in os.listdir(pkg_dir) if n.endswith(".go") and not n.endswith("_test.go"))
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(pkg_dir, names[0])


class GoModuleResolver:
    """Go ``module/path`` ⇄ package-directory resolution, anchored on ``go.mod``."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """The ``go.mod`` directories anchoring the touched Go files' module paths."""
        roots: set[str] = set()
        for f in abs_files:
            if not f.endswith(".go"):
                continue
            gomod = _find_go_mod_dir(os.path.dirname(f))
            if gomod is not None:
                roots.add(gomod)
        return roots

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """A repo-internal import path → a representative file in its package dir.

        For each ``go.mod`` root whose declared module prefixes *module*, the tail
        is the package directory relative to the root; the first ``.go`` file there
        represents the package. External paths (no matching prefix) → ``None``.
        """
        if not module:
            return None
        for root in roots:
            prefix = _module_prefix(root)
            if not prefix:
                continue
            if module == prefix:
                rel = ""
            elif module.startswith(prefix + "/"):
                rel = module[len(prefix) + 1 :]
            else:
                continue
            pkg_dir = os.path.join(root, rel) if rel else root
            if os.path.isdir(pkg_dir):
                found = _first_go_file(pkg_dir)
                if found is not None:
                    return found
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        """The import path *abspath*'s package is reachable by (via its ``go.mod``)."""
        if not abspath.endswith(".go"):
            return set()
        directory = os.path.dirname(abspath)
        gomod = _find_go_mod_dir(directory)
        if gomod is None:
            return set()
        prefix = _module_prefix(gomod)
        if not prefix:
            return set()
        rel = os.path.relpath(directory, gomod)
        if rel == ".":
            return {prefix}
        return {prefix + "/" + rel.replace(os.sep, "/")}

    def is_relative(self, module: str) -> bool:
        """Go has no relative imports — every import path is absolute."""
        return False


__all__ = ["GoModuleResolver"]
