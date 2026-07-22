"""Rust module ⇄ file arithmetic — crate-path + ``mod`` declaration resolution.

Rust links files two ways, and this resolver handles both: a ``use
crate::a::b`` path (crate-root-anchored — ``crate::`` is the crate's ``src``
directory, so ``crate::a::b`` → ``src/a/b.rs`` | ``src/a/b/mod.rs``) and a ``mod
name;`` declaration (a sibling module file resolved *relative to the declaring
file*, which the extractor has already turned into an absolute path stem). The
crate root is the ``src`` directory beside a ``Cargo.toml``. An external-crate
path (``std::fmt``, another crate) maps to no repo file → ``None`` (dropped, not
guessed).

TRADE (documented): the Rust-2018 nested-module convention (``mod bar`` inside
``foo.rs`` living at ``foo/bar.rs``) is not modeled — the common crate-root
``mod foo;`` → ``src/foo.rs`` case is. And ``use crate::m::Item`` (an item, not a
module, as the last segment) is handled by also probing the path minus its last
segment.
"""

from __future__ import annotations

import os
from typing import Optional

_CARGO = "Cargo.toml"
_CRATE_ROOTS = ("lib.rs", "main.rs")  # a src-root file *is* the crate module


def _src_root(abspath: str) -> Optional[str]:
    """The crate's ``src`` directory (beside the nearest ``Cargo.toml``), or None."""
    d = os.path.dirname(abspath)
    while True:
        if os.path.exists(os.path.join(d, _CARGO)):
            src = os.path.join(d, "src")
            return src if os.path.isdir(src) else d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _probe_stem(stem: str) -> Optional[str]:
    """A path stem → ``stem.rs`` | ``stem/mod.rs`` on disk, or None."""
    rs = stem + ".rs"
    if os.path.exists(rs):
        return rs
    mod = os.path.join(stem, "mod.rs")
    if os.path.exists(mod):
        return mod
    return None


class RustModuleResolver:
    """Rust ``crate::a::b`` / ``mod name`` ⇄ file resolution, anchored on ``Cargo.toml``."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """The crate ``src`` roots anchoring the touched Rust files' crate paths."""
        roots: set[str] = set()
        for f in abs_files:
            if not f.endswith(".rs"):
                continue
            src = _src_root(f)
            if src is not None:
                roots.add(src)
        return roots

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """A crate path (or absolute ``mod`` stem) → the file it names, or None."""
        if not module:
            return None
        # A ``mod name;`` declaration was pre-resolved to an absolute path stem.
        if os.path.isabs(module):
            return _probe_stem(module)
        # A ``crate::a::b`` path → <src>/a/b.rs | <src>/a/b/mod.rs.
        if module.startswith("crate::") or module == "crate":
            tail = module[len("crate") :].lstrip(":")
            segments = [s for s in tail.split("::") if s]
            for root in roots:
                if not segments:
                    hit = _probe_stem(os.path.join(root, "lib")) or _probe_stem(os.path.join(root, "main"))
                    if hit:
                        return hit
                    continue
                stem = os.path.join(root, *segments)
                hit = _probe_stem(stem)
                if hit:
                    return hit
                # ``use crate::m::Item`` — last segment is an item, not a module.
                if len(segments) > 1:
                    hit = _probe_stem(os.path.join(root, *segments[:-1]))
                    if hit:
                        return hit
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        """The crate path *abspath* is reachable by, plus its own absolute stem.

        The absolute stem lets a sibling's ``mod name;`` (pre-resolved to that
        stem) match; the ``crate::…`` spelling lets a ``use`` path match.
        """
        if not abspath.endswith(".rs"):
            return set()
        directory, filename = os.path.split(abspath)
        stem = filename[:-3]  # drop ".rs"
        cands: set[str] = {os.path.join(directory, stem)}
        if stem == "mod":
            cands.add(directory)  # ``foo/mod.rs`` is also reachable as the dir stem
        src = _src_root(abspath)
        if src is not None:
            base = directory if stem == "mod" else os.path.join(directory, stem)
            rel = os.path.relpath(base, src)
            if rel == ".":
                cands.add("crate")
            elif not rel.startswith(".."):
                parts = [p for p in rel.split(os.sep) if p]
                if filename in _CRATE_ROOTS:
                    cands.add("crate")
                else:
                    cands.add("crate::" + "::".join(parts))
        return cands

    def is_relative(self, module: str) -> bool:
        """A pre-resolved absolute ``mod`` stem is anchorable, not relative."""
        return False


__all__ = ["RustModuleResolver"]
