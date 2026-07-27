"""Stable, dependency-free docstring parsing contracts.

Leaf module — stdlib only (``inspect`` + ``re``). Used by both
``executor.tool_spec_adapter`` (``Args:`` → per-param descriptions) and
``executor.tasks.bggraph.base_node`` (``Params:`` → node param metadata).
"""

from __future__ import annotations

import inspect
from typing import Callable, Union

# Sections recognized as terminators when parsing a named section.
_KNOWN_SECTIONS = frozenset(
    {
        "args",
        "arguments",
        "params",
        "parameters",
        "returns",
        "return",
        "raises",
        "yields",
        "examples",
        "example",
        "note",
        "notes",
        "attributes",
        "todo",
        "references",
    }
)


def first_line(fn_or_doc: Union[Callable, str, None]) -> str:
    """Extract the first non-empty line of a docstring.

    Accepts a callable (reads its ``__doc__``) or a raw string.
    Returns ``""`` when the input is None or has no content.
    """
    if fn_or_doc is None:
        return ""
    if callable(fn_or_doc):
        doc = fn_or_doc.__doc__
    else:
        doc = fn_or_doc
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def description_body(fn_or_doc: Union[Callable, str, None]) -> str:
    """The model-facing description prose of a docstring — everything before the
    first Google-style section header (``Args:``/``Returns:``/…).

    This is the docstring's summary line plus its full operating manual, with the
    ``Args:`` block (and any other structured section) dropped so the parameter
    documentation is not duplicated as prose on the wire — parameters travel
    separately as a structured JSON Schema. Newlines/indentation inside the prose
    are preserved (via ``inspect.cleandoc``), so multi-paragraph manuals render
    intact. The FIRST line of the returned text is the tight one-line summary a
    tool-search menu shows (see :func:`first_line`); the whole text is the full
    description a tool sends once revealed.

    Accepts a callable (reads its ``__doc__``) or a raw string. Returns ``""``
    when there is no content.
    """
    if fn_or_doc is None:
        return ""
    doc = fn_or_doc.__doc__ if callable(fn_or_doc) else fn_or_doc
    if not doc:
        return ""
    out: list[str] = []
    for line in inspect.cleandoc(doc).splitlines():
        header = line.strip().rstrip(":").rstrip("\uff1a").lower()
        if header in _KNOWN_SECTIONS and line.strip().endswith((":", "\uff1a")):
            break
        out.append(line)
    return "\n".join(out).strip()


def parse_section(docstring: Union[str, None], section: str) -> list[tuple[str, str]]:
    """Parse a named section from a Google-style docstring.

    Finds the ``section:`` header (case-insensitive, supports full-width
    colon ``：``), then collects ``name: rest`` entries (with continuation
    lines) until the next known section header or end of docstring.

    Args:
        docstring: Raw docstring text (or None).
        section: Section name to find (e.g. "Args", "Params").

    Returns:
        Ordered list of ``(name, rest)`` tuples. Callers do further
        splitting (e.g. ``dict(...)`` or em-dash parse) as needed.
    """
    if not docstring:
        return []
    lines = inspect.cleandoc(docstring).splitlines()

    # Locate the section header.
    section_lower = section.lower()
    start_idx = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip().rstrip(":").rstrip("\uff1a").lower()
        if stripped == section_lower:
            start_idx = i
            break
    if start_idx < 0:
        return []

    # Collect entries.
    entries: list[tuple[str, str]] = []
    current_name: str | None = None
    current_rest: str = ""

    for raw in lines[start_idx + 1 :]:
        stripped = raw.strip()
        if not stripped:
            continue
        # Check for a new section header (terminates collection).
        maybe_header = stripped.rstrip(":").rstrip("\uff1a").lower()
        if maybe_header in _KNOWN_SECTIONS and stripped.endswith((":", "\uff1a")):
            break
        # Try to match a new entry: "name: rest" or "name (type): rest".
        head, sep, tail = stripped.partition(":")
        if sep and head:
            # The name is the part before any parenthetical type annotation.
            name_part = head.split("(")[0].strip()
            # A valid param name has no internal spaces.
            if name_part and " " not in name_part:
                # Flush previous entry.
                if current_name is not None:
                    entries.append((current_name, current_rest))
                current_name = name_part
                current_rest = tail.strip()
                continue
        # Continuation line — append to previous entry.
        if current_name is not None:
            current_rest = (current_rest + " " + stripped).strip()

    # Flush last entry.
    if current_name is not None:
        entries.append((current_name, current_rest))

    return entries
