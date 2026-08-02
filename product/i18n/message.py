#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A tiny zero-dependency ICU-subset message formatter.

Supported syntax (a deliberate subset of ICU MessageFormat, the shape Babel /
CLDR use — chosen so translators recognise it and a full-CLDR engine could drop
in later):

* literal text and ``{name}`` interpolation;
* ``{name, plural, =0{…} one{…} other{…}}`` where ``#`` renders the count;
* ``{name, select, key{…} other{…}}``;
* arbitrary nesting inside any case body.

Patterns parse once into a small AST (``lru_cache`` by the pattern string) and
render against ``(params, locale)`` — the locale supplies the CLDR plural
category so ``one``/``other`` are chosen correctly per language. Rendering never
raises on a missing param: an unknown ``{name}`` renders empty rather than
crashing a status line.

Number interpolation is intentionally *plain* (``str(value)``, no grouping) to
preserve the CLI's existing wording; locale grouping is an explicit call-site
decision, not implicit formatter behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, List, Mapping, Optional, Tuple

_SYNTAX = "{},#"


@dataclass(frozen=True)
class _Literal:
    text: str


@dataclass(frozen=True)
class _Var:
    name: str


@dataclass(frozen=True)
class _Hash:
    pass


@dataclass(frozen=True)
class _Plural:
    name: str
    cases: Tuple[Tuple[str, Tuple[Any, ...]], ...]


@dataclass(frozen=True)
class _Select:
    name: str
    cases: Tuple[Tuple[str, Tuple[Any, ...]], ...]


def _parse_nodes(s: str, i: int, stop_at_brace: bool) -> Tuple[Tuple[Any, ...], int]:
    """Parse a run of nodes from *i*; stop at a closing ``}`` when nested."""
    nodes: List[Any] = []
    buf: List[str] = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "}" and stop_at_brace:
            break
        if c == "#":
            if buf:
                nodes.append(_Literal("".join(buf)))
                buf = []
            nodes.append(_Hash())
            i += 1
            continue
        if c == "{":
            if buf:
                nodes.append(_Literal("".join(buf)))
                buf = []
            node, i = _parse_arg(s, i + 1)
            nodes.append(node)
            continue
        buf.append(c)
        i += 1
    if buf:
        nodes.append(_Literal("".join(buf)))
    return tuple(nodes), i


def _read_token(s: str, i: int) -> Tuple[str, int]:
    """Read up to the next ``,`` or ``}`` — an arg name or a plural/select type."""
    j = i
    n = len(s)
    while j < n and s[j] not in ",}":
        j += 1
    return s[i:j].strip(), j


def _parse_arg(s: str, i: int) -> Tuple[Any, int]:
    """Parse an argument starting just after ``{``; return (node, index-after-``}``)."""
    name, j = _read_token(s, i)
    if j >= len(s) or s[j] == "}":
        return _Var(name), j + 1
    typ, k = _read_token(s, j + 1)  # skip the first ','
    # k now points at the ',' before the case list (or a stray '}').
    if k < len(s) and s[k] == ",":
        k += 1
    cases, end = _parse_cases(s, k)
    if typ == "plural":
        return _Plural(name, cases), end
    return _Select(name, cases), end


def _parse_cases(s: str, i: int) -> Tuple[Tuple[Tuple[str, Tuple[Any, ...]], ...], int]:
    """Parse ``selector {body} …`` pairs until the arg's closing ``}``."""
    cases: List[Tuple[str, Tuple[Any, ...]]] = []
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] == "}":
            return tuple(cases), i + 1
        j = i
        while j < n and s[j] != "{":
            j += 1
        selector = s[i:j].strip()
        body, end = _parse_nodes(s, j + 1, stop_at_brace=True)
        cases.append((selector, body))
        i = end + 1  # skip the body's closing '}'
    return tuple(cases), n


@lru_cache(maxsize=512)
def _parse(pattern: str) -> Tuple[Any, ...]:
    nodes, _ = _parse_nodes(pattern, 0, stop_at_brace=False)
    return nodes


def _format_value(value: Any) -> str:
    """Plain interpolation — no locale grouping (see the module docstring)."""
    if isinstance(value, bool):  # avoid ``True`` leaking as ``1``/``0`` surprises
        return str(value)
    return str(value)


def _pick(cases: Tuple[Tuple[str, Tuple[Any, ...]], ...], selector: str) -> Optional[Tuple[Any, ...]]:
    """The body for an exact *selector*, or ``None`` (caller decides the fallback)."""
    for key, body in cases:
        if key == selector:
            return body
    return None


def _choose(cases: Tuple[Tuple[str, Tuple[Any, ...]], ...], *selectors: str) -> Tuple[Any, ...]:
    """First matching *selector* in priority order, else the ``other`` body, else empty."""
    for selector in selectors:
        body = _pick(cases, selector)
        if body is not None:
            return body
    return _pick(cases, "other") or ()


def _render(nodes: Tuple[Any, ...], params: Mapping[str, Any], locale: Any, number: Any) -> str:
    out: List[str] = []
    for node in nodes:
        if isinstance(node, _Literal):
            out.append(node.text)
        elif isinstance(node, _Hash):
            out.append(_format_value(number) if number is not None else "#")
        elif isinstance(node, _Var):
            out.append(_format_value(params[node.name]) if node.name in params else "")
        elif isinstance(node, _Plural):
            value = params.get(node.name, 0)
            category = locale.category(value) if locale is not None else ("one" if value == 1 else "other")
            out.append(_render(_choose(node.cases, f"={value}", category), params, locale, value))
        elif isinstance(node, _Select):
            value = params.get(node.name, "")
            out.append(_render(_choose(node.cases, _format_value(value)), params, locale, number))
    return "".join(out)


def format_message(pattern: str, params: Mapping[str, Any], locale: Any = None) -> str:
    """Render an ICU-subset *pattern* with *params* under *locale* (never raises)."""
    try:
        return _render(_parse(pattern), params, locale, None)
    except Exception:  # noqa: BLE001 — a display string must never crash the host
        return pattern


__all__ = ["format_message"]
