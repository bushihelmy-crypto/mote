"""Shared helpers for the declarative tree-sitter language configs.

Small utilities every language's import extractor reuses: a pre-order node
iterator, reading a quoted string literal's inner text, and resolving a *relative*
module specifier (``./util``, ``../lib/x``) against the importing file to the
absolute path *stem* it targets. Normalizing a relative specifier to an absolute
stem is what lets the language-neutral matcher in the facade string-compare it
against a file's :meth:`ModuleResolver.module_candidates` exactly as it already
does for Python's absolute dotted names — no per-language matcher branch.
"""

from __future__ import annotations

import os
from typing import Any, Iterator


def iter_nodes(node: Any) -> Iterator[Any]:
    """Pre-order walk over *node* and all its named descendants."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        # Reverse so children are yielded left-to-right (stack is LIFO).
        stack.extend(reversed(current.named_children))


def string_text(node: Any) -> str:
    """Inner text of a string literal node — quotes / fragments stripped.

    Handles the common tree-sitter shapes: a ``string`` wrapping a
    ``string_fragment`` child (JS/TS), or a bare string token whose text carries
    the surrounding quotes. Returns the unquoted body.
    """
    for child in node.named_children:
        if "fragment" in child.type or child.type == "string_content":
            return child.text.decode("utf-8", "replace")
    raw = node.text.decode("utf-8", "replace")
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def resolve_relative_stem(abspath: str, spec: str) -> str:
    """Absolute, extension-less path a relative *spec* targets from *abspath*.

    ``./util`` from ``/repo/src/main.js`` → ``/repo/src/util``; ``../lib/x`` →
    ``/repo/lib/x``. A trailing-slash / bare-directory spec keeps the directory
    (an index-file probe happens in the resolver). The stem is normalized so it
    string-matches a target file's module candidate.
    """
    base = os.path.dirname(abspath)
    joined = os.path.normpath(os.path.join(base, spec))
    return joined


def is_relative_spec(spec: str) -> bool:
    """True when *spec* is a relative module specifier (``.`` / ``..`` prefixed)."""
    return spec.startswith("./") or spec.startswith("../") or spec in (".", "..")


__all__ = ["iter_nodes", "string_text", "resolve_relative_stem", "is_relative_spec"]
