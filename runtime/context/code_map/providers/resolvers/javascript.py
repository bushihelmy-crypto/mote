"""JavaScript/TypeScript module ⇄ file arithmetic (ES-module + CommonJS paths).

JS resolution is *path*-based, not dotted like Python: a specifier is either
relative (``./util``, resolved to an absolute path stem by the import extractor
before it reaches here) or a bare package (``lodash`` — a ``node_modules`` name
we deliberately do not resolve, since it is not a repo-local file). So this
resolver works in absolute path *stems*: :meth:`module_to_path` probes a stem
against the known source extensions and an ``index.*`` directory entry, and
:meth:`module_candidates` turns a file back into the stem(s) an import could name
it by. This one resolver serves both JavaScript and TypeScript (the extension
list is the union), because a ``.ts`` file can import a ``.js`` sibling and vice
versa — the language boundary is not a resolution boundary in the JS ecosystem.
"""

from __future__ import annotations

import os
from typing import Optional

#: Source extensions a bare stem may resolve to, most-specific first. Union of
#: JS + TS so cross-language sibling imports resolve either way.
_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class JsModuleResolver:
    """Path-stem ⇄ file resolution for the JavaScript/TypeScript family."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """Directory anchors — unused for path-stem resolution (kept for protocol).

        JS specifiers resolve by absolute path stem (already anchored by the
        import extractor), so no root inference is needed; the containing dirs are
        returned harmlessly for any future bare-package handling.
        """
        return {os.path.dirname(f) for f in abs_files}

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """Map an absolute path *stem* to a real source file (or an ``index.*``).

        ``/repo/src/util`` → ``/repo/src/util.ts`` | ``…/util.js`` | … , else
        ``/repo/src/util/index.ts`` | … . A bare (non-absolute) module is a
        ``node_modules`` package we do not resolve → ``None``.
        """
        if not module or not os.path.isabs(module):
            return None  # bare package (node_modules) — not a repo-local file
        for ext in _EXTENSIONS:
            candidate = module + ext
            if os.path.exists(candidate):
                return candidate
        for ext in _EXTENSIONS:
            index = os.path.join(module, "index" + ext)
            if os.path.exists(index):
                return index
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        """Absolute path stems an import could name *abspath* by.

        ``/repo/src/util.js`` → ``{/repo/src/util}``. An ``index`` file also
        offers its directory (``/repo/src/index.js`` → ``{…/index, /repo/src}``),
        so ``import "./src"`` resolves to it.
        """
        directory, filename = os.path.split(abspath)
        stem, ext = os.path.splitext(filename)
        if ext not in _EXTENSIONS:
            return set()
        cands = {os.path.join(directory, stem)}
        if stem == "index":
            cands.add(directory)
        return cands

    def is_relative(self, module: str) -> bool:
        """Never relative here — the extractor resolves ``./x`` to an absolute stem.

        By the time a specifier reaches the facade it is either an absolute stem
        (anchored) or a bare package; neither is a leading-dot relative spelling,
        so the dangling-import path treats an absolute stem as anchorable (it maps
        to a repo file) and a bare package as external (maps to nothing).
        """
        return False


__all__ = ["JsModuleResolver"]
