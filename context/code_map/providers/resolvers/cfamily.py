"""C / C++ ``#include`` ⇄ file arithmetic — quoted-include path resolution.

C's only cross-file link is the preprocessor ``#include``, and it comes in two
flavors with different search semantics: a *quoted* include ``#include "util.h"``
is resolved **relative to the including file** first (the portable, repo-local
case), while an *angle-bracket* include ``#include <stdio.h>`` names a system /
toolchain header found only via ``-I`` search paths the compiler knows and a
static reader does not. So the extractor resolves ``"util.h"`` to the absolute
path it targets (extension kept — a header names a real file, not a stem) and
drops ``<…>`` entirely; this resolver then just confirms that absolute path
exists. An unresolved / system include maps to no repo file → ``None`` (dropped,
not guessed — a wrong cross-file edge is worse than a missing one).

TRADE (documented): only the includer-relative form of a quoted include is
modeled. A quoted include that only resolves via a project ``-I`` root (e.g.
``#include "core/foo.h"`` found under an added include dir) is not — the include
roots are a build-system fact, not derivable from the source. The common
same-tree ``#include "sibling.h"`` case is exact.
"""

from __future__ import annotations

import os
from typing import Optional


class CIncludeResolver:
    """``#include "x.h"`` ⇄ file resolution — quoted includes only, by absolute path."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """Directory anchors — unused for absolute-path resolution (kept for protocol).

        A quoted include is already resolved to an absolute path by the extractor,
        so no root inference is needed; the containing dirs are returned harmlessly.
        """
        return {os.path.dirname(f) for f in abs_files}

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """An absolute include path → the header file it names, or ``None``.

        The extractor hands over quoted includes already resolved to an absolute
        path (extension kept); a system ``<…>`` include was dropped and never
        reaches here, so a non-absolute / missing key resolves to nothing.
        """
        if not module or not os.path.isabs(module):
            return None  # system include (dropped) / unanchorable — not a repo file
        return module if os.path.exists(module) else None

    def module_candidates(self, abspath: str) -> set[str]:
        """The include key(s) *abspath* is reachable by — its own absolute path.

        A header is named by the absolute path a quoted include resolves to, so a
        file's sole candidate is its own path (string-matched against the resolved
        include keys of files that include it).
        """
        return {abspath}

    def is_relative(self, module: str) -> bool:
        """Never relative here — the extractor resolves ``"x.h"`` to an absolute path.

        By the time a key reaches the facade it is either an absolute path
        (anchorable — maps to a repo header) or was dropped (system include); no
        leading-dot relative spelling survives.
        """
        return False


__all__ = ["CIncludeResolver"]
