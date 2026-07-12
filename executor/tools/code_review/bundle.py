"""Deterministic related-file bundling for the code-review pipeline.

Given the full set of changed files in a diff, attach to each reviewable file a
short list of *related* paths worth reading for context. The reviewer agent
already recalls context dynamically via its tools, so this is only a hint — a
cheap, deterministic nudge toward the files most likely to matter:

1. **Test ↔ impl pairing** — ``foo.py`` ↔ ``test_foo.py``; ``h.go`` ↔
   ``h_test.go``; ``x.ts`` ↔ ``x.test.ts`` / ``x.spec.ts``. A changed test is
   excellent context for its implementation (and vice-versa) even though the
   test itself is filtered out of review.
2. **Same-stem siblings** — ``foo.py`` & ``foo.pyi``; ``a.c`` & ``a.h``.
3. **Same-directory co-changes** — other files that changed in the same
   directory (capped, lowest priority).

Candidates are drawn from the *whole* changed-file set (including files the
filter excludes from review, like tests), since those are exactly the useful
neighbours. No LLM, no filesystem access.
"""
from __future__ import annotations

import os
from typing import Dict, List

from .parser import FileDiff

# Extensions whose stems pair as test ↔ impl via a filename affix.
_TEST_PREFIXES = ("test_",)
_TEST_SUFFIXES = ("_test", ".test", ".spec")

_MAX_RELATED = 5


def _stem_and_ext(path: str) -> tuple[str, str]:
    base = os.path.basename(path)
    root, ext = os.path.splitext(base)
    return root, ext


def _impl_stem(stem: str) -> str:
    """Strip a test affix from *stem* → the implementation stem it pairs with.

    ``test_foo`` → ``foo``; ``foo_test`` → ``foo``; ``foo.test`` → ``foo``.
    Returns the stem unchanged when no affix is present.
    """
    for pre in _TEST_PREFIXES:
        if stem.startswith(pre) and len(stem) > len(pre):
            return stem[len(pre):]
    for suf in _TEST_SUFFIXES:
        if stem.endswith(suf) and len(stem) > len(suf):
            return stem[: -len(suf)]
    return stem


def attach_related(files: List[FileDiff], *, max_related: int = _MAX_RELATED) -> None:
    """Populate ``file.related`` for every file in *files* (in place).

    *files* is the full parsed changeset (reviewable or not). Each file's
    ``related`` is filled with up to *max_related* sibling paths, ordered by
    heuristic priority (test/impl pair → same-stem sibling → same-dir co-change).
    """
    # Index by directory and by implementation-stem for O(1) lookups.
    by_dir: Dict[str, List[str]] = {}
    by_impl_stem: Dict[str, List[str]] = {}
    for f in files:
        d = os.path.dirname(f.path)
        by_dir.setdefault(d, []).append(f.path)
        stem, _ext = _stem_and_ext(f.path)
        by_impl_stem.setdefault(_impl_stem(stem), []).append(f.path)

    for f in files:
        d = os.path.dirname(f.path)
        stem, ext = _stem_and_ext(f.path)
        impl = _impl_stem(stem)

        ordered: List[str] = []

        def _add(path: str) -> None:
            if path != f.path and path not in ordered:
                ordered.append(path)

        # 1. Test ↔ impl pairs: same implementation-stem, same directory.
        for cand in by_impl_stem.get(impl, []):
            if os.path.dirname(cand) == d:
                _add(cand)

        # 2. Same-stem siblings in the same directory (different extension).
        for cand in by_dir.get(d, []):
            cstem, cext = _stem_and_ext(cand)
            if cstem == stem and cext != ext:
                _add(cand)

        # 3. Same-directory co-changes (lowest priority).
        for cand in by_dir.get(d, []):
            _add(cand)

        f.related = ordered[:max_related]


__all__ = ["attach_related"]
