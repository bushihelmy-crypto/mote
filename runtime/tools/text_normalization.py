"""Deterministic whitespace normalization.

The idiom ``" ".join(text.split())`` (equivalently ``re.sub(r"\\s+", " ",
text).strip()``) was re-derived in three unrelated leaves — ``remove_spaces`` in
logging, tool parsing, and product workflow views. Runtime is the lowest
consumer layer.

Zero dependencies beyond the stdlib; no I/O, no provider shapes, no rendering.
"""
from __future__ import annotations


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace (incl. newlines/tabs) to a single space.

    Leading/trailing whitespace is dropped. Flattens multi-line text onto one
    line — use only where a single-line rendering is wanted.
    """
    return " ".join(text.split())


__all__ = ["collapse_whitespace"]
