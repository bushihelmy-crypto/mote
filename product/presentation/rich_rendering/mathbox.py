#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2D terminal math layout — a tiny TeX-style box model.

A terminal is a character grid; math wants 2D placement (a fraction stacks its
numerator over a rule over its denominator; a matrix stacks rows inside tall
brackets). KaTeX solves the *browser* version by laying out boxes carrying a
height/depth relative to a baseline and gluing them together on that axis. We
borrow only that idea — a :class:`MathBox` is a rectangle of equal-width text
rows plus the index of the row that sits on the math axis — and compose a
formula by aligning boxes on their baseline.

Parsing is delegated to :mod:`pylatexenc` (``latexwalker`` gives us the AST for
free); this module does only *layout*. So the two responsibilities stay apart:
adding a construct means adding one combinator + one walker branch, and anything
the walker doesn't recognise falls back to the flat single-line Unicode
conversion — output is therefore never *worse* than the one-line renderer.

Two output modes share one walk, switched by the ``display`` flag:

* ``display=False`` (inline, ``$…$``) — every box is forced to height 1 so it
  flows inside a sentence: a fraction becomes ``(a+b)/(c-d)`` (parenthesised
  only when compound, so it can't read ambiguously), a matrix ``[a b ; c d]``.
* ``display=True`` (block, ``$$…$$``) — fractions and matrices lay out over
  multiple rows, the elegant 2D form.

:func:`flatten` (single line) and :func:`build_box` (full box) are the module's
surface; both return ``None`` when pylatexenc is missing or the fragment can't
be parsed, so callers can leave the raw source untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.cells import cell_len

try:
    from pylatexenc.latex2text import LatexNodes2Text
    from pylatexenc.latexwalker import (
        LatexCharsNode,
        LatexEnvironmentNode,
        LatexGroupNode,
        LatexMacroNode,
        LatexSpecialsNode,
        LatexWalker,
    )
except ImportError:  # pragma: no cover - optional cli extra
    LatexNodes2Text = None
    LatexCharsNode = None
    LatexEnvironmentNode = None
    LatexGroupNode = None
    LatexMacroNode = None
    LatexSpecialsNode = None
    LatexWalker = None

# ``^``/``_`` scripts we can map to real Unicode super/subscript glyphs. Sourced
# from the same code-point set KaTeX ships (``unicodeSupOrSub``): every letter /
# digit / operator that HAS a dedicated codepoint, so a run maps fully or is left
# raw rather than mangled. This is the one piece of KaTeX genuinely reusable for a
# terminal — the glyph table, not its HTML typesetting.
_SUPERSCRIPTS = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "a": "ᵃ",
    "b": "ᵇ",
    "c": "ᶜ",
    "d": "ᵈ",
    "e": "ᵉ",
    "f": "ᶠ",
    "g": "ᵍ",
    "h": "ʰ",
    "i": "ⁱ",
    "j": "ʲ",
    "k": "ᵏ",
    "l": "ˡ",
    "m": "ᵐ",
    "n": "ⁿ",
    "o": "ᵒ",
    "p": "ᵖ",
    "r": "ʳ",
    "s": "ˢ",
    "t": "ᵗ",
    "u": "ᵘ",
    "v": "ᵛ",
    "w": "ʷ",
    "x": "ˣ",
    "y": "ʸ",
    "z": "ᶻ",
    "A": "ᴬ",
    "B": "ᴮ",
    "D": "ᴰ",
    "E": "ᴱ",
    "G": "ᴳ",
    "H": "ᴴ",
    "I": "ᴵ",
    "J": "ᴶ",
    "K": "ᴷ",
    "L": "ᴸ",
    "M": "ᴹ",
    "N": "ᴺ",
    "O": "ᴼ",
    "P": "ᴾ",
    "R": "ᴿ",
    "T": "ᵀ",
    "U": "ᵁ",
    "V": "ⱽ",
    "W": "ᵂ",
}
_SUBSCRIPTS = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
}
# ``^{…}`` / ``_{…}`` group or a single ``^x`` / ``_\alpha`` argument.
_SCRIPT_RE = re.compile(r"([\^_])(?:\{([^{}]*)\}|(\\?\w))")
# A script "token": a macro (``\pi``) or a single symbol — used to decide whether
# an unmappable script body is COMPOUND (parenthesise) or a lone symbol (leave).
_TOKEN_RE = re.compile(r"\\[A-Za-z]+|[^\s]")
# The glyphs a run may ALREADY contain (identity passthrough) — so a nested
# script like ``x^2`` inside ``e^{…}`` (already ``x²`` after inner conversion)
# still maps: the ``²`` is a superscript glyph, so it stays put rather than
# aborting the whole run. Without this ``e^{x^2}`` degrades to ``e^x^2``.
_SUPER_VALUES = frozenset(_SUPERSCRIPTS.values())
_SUB_VALUES = frozenset(_SUBSCRIPTS.values())
# Consecutive ``'`` are derivative primes; latex2text mangles ``''`` into a
# curly quote (``”``) — map to the real prime glyphs instead.
_PRIME_RE = re.compile(r"'+")
_PRIMES = {1: "′", 2: "″", 3: "‴", 4: "⁗"}


def _map_run(run: str, table: dict) -> str | None:
    """Map every char of *run* through *table*, or ``None`` if any is unmappable.

    Structural braces (from a recursively-converted nested script) are dropped,
    and a char that is ALREADY a glyph of *table* passes through unchanged, so a
    nested exponent (``x²`` under ``^{…}``) maps to ``ˣ²`` instead of failing.
    """
    values = _SUPER_VALUES if table is _SUPERSCRIPTS else _SUB_VALUES
    out: list[str] = []
    for ch in run:
        if ch in "{}":
            continue
        glyph = table.get(ch)
        if glyph is not None:
            out.append(glyph)
        elif ch in values:
            out.append(ch)
        else:
            return None
    return "".join(out)


def _convert_primes(latex: str) -> str:
    """Rewrite runs of ``'`` to derivative prime glyphs (``′``/``″``/``‴``)."""
    return _PRIME_RE.sub(lambda m: _PRIMES.get(len(m.group()), "′" * len(m.group())), latex)


def _convert_scripts(latex: str) -> str:
    """Rewrite ``^{…}`` / ``_{…}`` groups to Unicode super/subscripts in *latex*.

    Runs on the raw LaTeX (before symbol expansion) so brace grouping is intact —
    ``2^{10}`` becomes ``2¹⁰`` (not the lossy ``2¹0`` you'd get post-flattening).
    An empty ``{}`` is emitted ahead of the glyph so a preceding macro name
    (``\\sum_i``) doesn't swallow the Unicode char when the rest is flattened.
    """

    def repl(m: re.Match) -> str:
        table = _SUPERSCRIPTS if m.group(1) == "^" else _SUBSCRIPTS
        body = m.group(2) if m.group(2) is not None else m.group(3)
        # Recurse first so a nested script (``x^2`` inside ``^{…}``) is already
        # ``x²`` before this run is mapped — otherwise the inner ``^`` aborts it.
        mapped = _map_run(_convert_scripts(body), table)
        if mapped is not None:
            return "{}" + mapped
        # Unmappable and COMPOUND (e.g. ``^{i\pi}`` — ``π`` has no glyph): wrap in
        # parens so it reads as ``e^(iπ)`` instead of leaking a bare ``e^iπ``. A
        # single symbol (``x^q``) stays bare — parens there would just add noise.
        if len(_TOKEN_RE.findall(body)) > 1:
            return f"{m.group(1)}({body})"
        return m.group(0)

    return _SCRIPT_RE.sub(repl, latex)


def flatten(latex: str) -> str | None:
    """Flatten a LaTeX math *latex* fragment to a single line of Unicode text.

    ``x^2`` → ``x²``, ``\\alpha`` → ``α``, ``\\frac{a}{b}`` → ``a/b``. Returns
    ``None`` when pylatexenc is unavailable or the fragment fails to parse, so the
    caller can leave the original span untouched (never worse than the raw source).
    """
    if LatexNodes2Text is None:
        return None
    try:
        text = LatexNodes2Text().latex_to_text(_convert_primes(_convert_scripts(latex)))
    except Exception:
        return None
    # Math is single-line: collapse the whitespace pylatexenc emits around a
    # multiline display block so it reads as one flowing formula.
    return " ".join(text.split())


# --- MathBox: a rectangle of text rows aligned on a baseline -----------------


def _rpad(line: str, width: int) -> str:
    """Right-pad *line* with spaces to *width* display cells."""
    gap = width - cell_len(line)
    return line + " " * gap if gap > 0 else line


def _center(line: str, width: int) -> str:
    """Centre *line* within *width* display cells."""
    gap = width - cell_len(line)
    if gap <= 0:
        return line
    left = gap // 2
    return " " * left + line + " " * (gap - left)


@dataclass(frozen=True)
class MathBox:
    """A rectangle of equal-width text rows plus the row index on the math axis.

    ``lines`` are already padded to a common display width (:attr:`width`);
    ``baseline`` is the row that sits on the math axis, so boxes glue together by
    lining that row up (see :func:`hconcat`).
    """

    lines: tuple[str, ...]
    baseline: int
    width: int

    @property
    def height(self) -> int:
        return len(self.lines)

    def to_line(self) -> str:
        """The single-line form (join rows with a space if somehow multi-row)."""
        return self.lines[0] if self.height == 1 else " ".join(s.strip() for s in self.lines)

    def render_lines(self) -> tuple[str, ...]:
        """Rows padded to a clean rectangle (for painting a background box)."""
        return tuple(_rpad(line, self.width) for line in self.lines)


def atom(text: str) -> MathBox:
    """A single-row box holding *text*."""
    return MathBox((text,), 0, cell_len(text))


def hconcat(boxes: list[MathBox]) -> MathBox:
    """Glue *boxes* left-to-right, aligning them on their baselines."""
    boxes = [b for b in boxes if b.width or b.height > 1]
    if not boxes:
        return atom("")
    if len(boxes) == 1:
        return boxes[0]
    above = max(b.baseline for b in boxes)
    below = max(b.height - 1 - b.baseline for b in boxes)
    height = above + below + 1
    rows = [""] * height
    for b in boxes:
        top = above - b.baseline
        for i in range(height):
            j = i - top
            rows[i] += _rpad(b.lines[j], b.width) if 0 <= j < b.height else " " * b.width
    return MathBox(tuple(rows), above, sum(b.width for b in boxes))


def frac(num: MathBox, den: MathBox) -> MathBox:
    """Stack *num* over *den* separated by a ``───`` rule (the fraction bar)."""
    width = max(num.width, den.width, 1)
    lines = tuple(_center(ln, width) for ln in num.lines)
    lines += ("─" * width,)
    lines += tuple(_center(ln, width) for ln in den.lines)
    return MathBox(lines, num.height, width)


def frac_inline(num: MathBox, den: MathBox) -> MathBox:
    """Single-line fraction ``a/b``; parenthesise a compound part to stay unambiguous."""

    def wrap(s: str) -> str:
        return f"({s})" if any(c in s for c in "+-*/ ") else s

    return atom(f"{wrap(num.to_line())}/{wrap(den.to_line())}")


def sqrt(body: MathBox, index: str = "") -> MathBox:
    """Draw *body* under a radical: a ``√`` checkmark with an overline vinculum.

    The vinculum (``───`` overline) extends across the full width of *body*, so a
    tall radicand (a fraction, a stacked expression) reads as one enclosed span —
    the "stereoscopic" 2D radical. The ``√`` sits on the radicand's baseline row;
    rows above it get blank gutter so the checkmark reads as the radical's foot.
    An optional *index* (the ``n`` of an n-th root) is superscripted before ``√``.
    """
    width = body.width
    idx = (_map_run(index, _SUPERSCRIPTS) or index) if index else ""
    stem = f"{idx}\u221a"  # (optional superscript index) + √
    pad = cell_len(stem)
    # Overline sits one column left of the radicand and covers it fully; the extra
    # cell is where the √ checkmark's arm meets the vinculum.
    vinculum = " " * pad + "\u2500" * (width + 1)
    rows: list[str] = []
    for i, line in enumerate(body.lines):
        gutter = (stem + " ") if i == body.baseline else " " * (pad + 1)
        rows.append(gutter + _rpad(line, width))
    lines = (vinculum,) + tuple(rows)
    return MathBox(lines, body.baseline + 1, pad + 1 + width)


def sqrt_inline(body: MathBox, index: str = "") -> MathBox:
    """Single-line radical ``√(a+b)``; parenthesise a compound radicand for clarity."""
    inner = body.to_line()
    wrapped = f"({inner})" if any(c in inner for c in "+-*/ ") else inner
    idx = (_map_run(index, _SUPERSCRIPTS) or index) if index else ""
    return atom(f"{idx}\u221a{wrapped}")


# Stretchy bracket glyphs by matrix delimiter kind (top / middle / bottom).
_BRACKETS = {
    "(": ("⎛", "⎜", "⎝"),
    "[": ("⎡", "⎢", "⎣"),
    "|": ("│", "│", "│"),
}
_BRACKETS_R = {
    ")": ("⎞", "⎟", "⎠"),
    "]": ("⎤", "⎥", "⎦"),
    "|": ("│", "│", "│"),
}


def _bracket_col(height: int, glyphs: tuple[str, str, str], flat: str) -> list[str]:
    """A vertical bracket column *height* rows tall from (top, mid, bottom) glyphs."""
    if height == 1:
        return [flat]
    top, mid, bot = glyphs
    return [top] + [mid] * (height - 2) + [bot]


def matrix(rows: list[list[MathBox]], left: str, right: str) -> MathBox:
    """Lay out a grid of cell boxes inside stretchy *left*/*right* brackets."""
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return atom(f"{left}{right}")
    colw = [max((r[c].width if c < len(r) else 0 for r in rows), default=0) for c in range(ncols)]
    row_boxes: list[MathBox] = []
    for r in rows:
        cells: list[MathBox] = []
        for c in range(ncols):
            b = r[c] if c < len(r) else atom("")
            padded = tuple(_center(line, colw[c]) for line in b.lines)
            cells.append(MathBox(padded, b.baseline, colw[c]))
            if c < ncols - 1:
                cells.append(atom("  "))  # column gap
        row_boxes.append(hconcat(cells))
    grid_w = max(b.width for b in row_boxes)
    grid: list[str] = []
    for rb in row_boxes:
        grid.extend(_rpad(line, grid_w) for line in rb.lines)
    height = len(grid)
    lcol = _bracket_col(height, _BRACKETS.get(left, _BRACKETS["["]), left)
    rcol = _bracket_col(height, _BRACKETS_R.get(right, _BRACKETS_R["]"]), right)
    lines = tuple(f"{lc} {g} {rc}" for lc, g, rc in zip(lcol, grid, rcol))
    return MathBox(lines, height // 2, grid_w + 4)


def matrix_inline(rows: list[list[MathBox]], left: str, right: str) -> MathBox:
    """Single-line matrix ``[ a b ; c d ]`` (rows joined by ``;``)."""
    body = " ; ".join(" ".join(cell.to_line() for cell in r) for r in rows)
    return atom(f"{left} {body} {right}")


def _stack_limits(sym: MathBox, sub: MathBox | None, sup: MathBox | None) -> MathBox:
    """Stack *sup* over *sym* over *sub*, each centred — the display big-operator.

    ``\\sum``/``\\lim`` in display math put their limits directly above and below
    the symbol; the operator row is the baseline so it glues to the rest of the
    formula on that axis.
    """
    width = max(sym.width, sub.width if sub else 0, sup.width if sup else 0, 1)
    lines: list[str] = []
    baseline = 0
    if sup and sup.width:
        lines += [_center(ln, width) for ln in sup.lines]
        baseline = sup.height
    lines += [_center(ln, width) for ln in sym.lines]
    if sub and sub.width:
        lines += [_center(ln, width) for ln in sub.lines]
    return MathBox(tuple(lines), baseline + sym.baseline, width)


# A single integral drawn tall from the three integral-sign pieces; the middle
# extension row is the baseline so the integrand lines up with it.
_TALL_INTEGRAL = ("⌠", "⎮", "⌡")


def _side_limits(sym: MathBox, sub: MathBox | None, sup: MathBox | None, macroname: str = "int") -> MathBox:
    """Draw an integral with limits at the top-/bottom-right of a tall sign.

    A plain ``\\int_a^b`` typesets as a stretched sign (``⌠⎮⌡``) carrying ``b`` by
    its top and ``a`` by its foot, the integrand riding the middle (baseline) row
    — the classic display-integral shape, far cleaner than a flat ``∫`` with side
    scripts. Multi/contour integrals (``∬``/``∮``) keep their own single glyph
    (already tall) with the limits set beside it.
    """
    sup_line = sup.to_line() if sup else ""
    sub_line = sub.to_line() if sub else ""
    if not sup_line and not sub_line:
        return _op_symbol(macroname, display=True)
    if macroname == "int":
        top, mid, bot = _TALL_INTEGRAL
    else:
        glyph = flatten("\\" + macroname) or macroname
        top, mid, bot = " ", glyph, " "
    # Reserve one column per sign glyph + the widest limit, so the integrand on
    # the middle row starts clear of a wide bound (``⌠¹⁰`` won't collide with it).
    limw = max(cell_len(sup_line), cell_len(sub_line))
    lines = [
        top + _rpad(sup_line, limw),
        mid + " " * limw,
        bot + _rpad(sub_line, limw),
    ]
    return MathBox(tuple(lines), 1, 1 + limw)


# --- Walker: pylatexenc AST → MathBox ----------------------------------------

_MATRIX_DELIMS = {
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "Bmatrix": ("[", "]"),
    "vmatrix": ("|", "|"),
    "Vmatrix": ("|", "|"),
    "matrix": (" ", " "),
}
# ``\\`` renders as a macro whose name is a single space; ``cr``/``newline`` are aliases.
_ROW_BREAK = {"\\", " ", "cr", "newline"}

# Big operators that take limits. In DISPLAY math the limits stack above/below
# the symbol (``STACKED``); integrals keep them beside the sign (``SIDE``). The
# text-operators (``\lim`` etc.) render as words but still stack their limit.
_STACKED_OPS = {
    "sum",
    "prod",
    "coprod",
    "bigcup",
    "bigcap",
    "bigvee",
    "bigwedge",
    "bigoplus",
    "bigotimes",
    "bigodot",
    "biguplus",
    "bigsqcup",
    "lim",
    "limsup",
    "liminf",
    "max",
    "min",
    "sup",
    "inf",
    "gcd",
    "det",
    "Pr",
}
_SIDE_OPS = {"int", "iint", "iiint", "iiiint", "oint", "oiint", "oiiint"}
_BIG_OPS = _STACKED_OPS | _SIDE_OPS


def _group_nodelist(node) -> list:
    """The inner nodelist of a group/argument node (``{…}``), else the node itself."""
    inner = getattr(node, "nodelist", None)
    return list(inner) if inner is not None else [node]


def _split_matrix(env_node) -> list[list[list]]:
    """Split a matrix environment body into rows of cells, each a nodelist."""
    rows: list[list[list]] = []
    cur_row: list[list] = []
    cur_cell: list = []
    for n in env_node.nodelist:
        if isinstance(n, LatexMacroNode) and n.macroname in _ROW_BREAK:
            cur_row.append(cur_cell)
            rows.append(cur_row)
            cur_row, cur_cell = [], []
        elif isinstance(n, LatexSpecialsNode) and n.specials_chars == "&":
            cur_row.append(cur_cell)
            cur_cell = []
        else:
            cur_cell.append(n)
    cur_row.append(cur_cell)
    rows.append(cur_row)
    # Drop wholly-empty trailing rows (a trailing ``\\`` leaves one).
    return [r for r in rows if any(_flat_nodes(c) for c in r)]


def _flat_nodes(nodes: list) -> str:
    """Verbatim LaTeX of *nodes* joined, for the flat-Unicode fallback path."""
    return "".join(n.latex_verbatim() for n in nodes)


def _op_symbol(macroname: str, *, display: bool) -> MathBox:
    """The rendered big-operator symbol (``∑``/``∫``/``lim`` …) as a box."""
    text = flatten("\\" + macroname) or macroname
    return atom(text)


def _big_operator(nodes: list, i: int, *, display: bool) -> tuple[int, MathBox | None]:
    """Parse a big operator at ``nodes[i]`` plus its ``_``/``^`` limits.

    Returns ``(next_index, box)`` — ``box`` is the operator with its limits
    stacked (``\\sum``/``\\lim``) or set beside it (``\\int``) in display math, or
    subscript/superscript inline; or ``None`` (index unadvanced) when there are
    no limit scripts, so the caller falls through to the ordinary flat path.
    """
    op = nodes[i]
    macroname = op.macroname
    j = i + 1
    sub_nodes: list | None = None
    sup_nodes: list | None = None

    # Scripts arrive two ways: as ``_``/``^`` CharsNode + a following GroupNode
    # (``\lim_{x\to0}``), or fused into one CharsNode (``\int_0^1 f`` → ``_0^1 f``).
    def take_group_script(mark: str):
        nonlocal j
        if (
            j < len(nodes)
            and isinstance(nodes[j], LatexCharsNode)
            and nodes[j].chars == mark
            and j + 1 < len(nodes)
            and isinstance(nodes[j + 1], LatexGroupNode)
        ):
            body = _group_nodelist(nodes[j + 1])
            j += 2
            return body
        return None

    for _ in range(2):
        g = take_group_script("_")
        if g is not None:
            sub_nodes = g
            continue
        g = take_group_script("^")
        if g is not None:
            sup_nodes = g
            continue
        break

    # Fused form: a CharsNode beginning with ``_``/``^`` — either the whole
    # limit set (``\int_0^1 f`` → ``_0^1 f``) or the remaining half after a group
    # script was already consumed (``\prod_{i=1}^n`` → group ``_`` then ``^n``).
    sub_str = sup_str = ""
    trailing = ""
    if (
        (sub_nodes is None or sup_nodes is None)
        and j < len(nodes)
        and isinstance(nodes[j], LatexCharsNode)
        and nodes[j].chars[:1] in ("_", "^")
    ):
        sub_str, sup_str, trailing, dangling = _parse_fused_scripts(nodes[j].chars)
        # A group-parsed half wins; the fused string only fills what's missing.
        if sub_nodes is not None:
            sub_str = ""
        if sup_nodes is not None:
            sup_str = ""
        if sub_str or sup_str or dangling:
            j += 1
        # A dangling ``_``/``^`` binds the FOLLOWING node as its bound
        # (``\int_0^\infty`` → fused ``_0^`` then a ``\infty`` macro node).
        if dangling and j < len(nodes):
            bound = flatten(nodes[j].latex_verbatim()) or nodes[j].latex_verbatim()
            if dangling == "_":
                sub_str = bound
            else:
                sup_str = bound
            j += 1

    if sub_nodes is None and sup_nodes is None and not sub_str and not sup_str:
        return i, None  # bare operator, nothing to stack

    sym = _op_symbol(macroname, display=True)
    sub = _script_box(sub_nodes, sub_str)
    sup = _script_box(sup_nodes, sup_str)
    if not display:
        # Inline: only take over when a script can't map to Unicode glyphs (the
        # leak case, e.g. ``\lim_{x\to0}``). When both scripts are mappable, the
        # ordinary flat path renders ``∑ᵢ₌₁ⁿ`` correctly AND keeps the space to
        # following content, so let it handle those — return None to fall through.
        sub_s = sub.to_line() if sub else ""
        sup_s = sup.to_line() if sup else ""
        sub_ok = not sub_s or _map_run(sub_s, _SUBSCRIPTS) is not None
        sup_ok = not sup_s or _map_run(sup_s, _SUPERSCRIPTS) is not None
        if sub_ok and sup_ok:
            return i, None
        box = _inline_limits(macroname, sym, sub, sup)
    elif macroname in _SIDE_OPS:
        box = _side_limits(sym, sub, sup, macroname)
    else:
        box = _stack_limits(sym, sub, sup)
    if trailing.strip():
        tail = trailing.strip()
        box = hconcat([box, atom(" " + (flatten(tail) or tail))])
    return j, box


def _inline_limits(macroname: str, sym: MathBox, sub: MathBox | None, sup: MathBox | None) -> MathBox:
    """One-line big operator: Unicode super/subscript when mappable, else ``(…)``.

    ``\\sum_{i=1}^{n}`` → ``∑ᵢ₌₁ⁿ`` (glyphs available); ``\\lim_{x\\to0}`` →
    ``lim(x→0)`` since ``x→0`` has no subscript glyphs — the paren form keeps the
    bound legibly attached to the operator instead of leaking a raw ``_``.
    """
    text = sym.to_line()
    sub_s = sub.to_line() if sub else ""
    sup_s = sup.to_line() if sup else ""
    sub_g = _map_run(sub_s, _SUBSCRIPTS) if sub_s else ""
    sup_g = _map_run(sup_s, _SUPERSCRIPTS) if sup_s else ""
    # When BOTH bounds need parens (e.g. ``\int_{-\infty}^{+\infty}``) mark them
    # symmetrically as ``_(…)^(…)`` so sub and sup read consistently; a word
    # operator like ``\lim`` has only a sub, so its bare ``(x→0)`` attaches cleanly.
    both_paren = (sub_s and sub_g is None) and (sup_s and sup_g is None)
    if sub_s and sub_g is None:
        text += f"_({sub_s})" if both_paren else f"({sub_s})"
        sub_s = ""
    else:
        text += sub_g or ""
    if sup_s and sup_g is None:
        text += f"^({sup_s})"
    else:
        text += sup_g or ""
    return atom(text)


def _script_box(nodelist: list | None, text: str) -> MathBox | None:
    """A limit box from either a parsed nodelist or a raw fused string."""
    if nodelist is not None:
        flat = flatten(_flat_nodes(nodelist)) or _flat_nodes(nodelist)
        return atom(flat)
    if text:
        return atom(flatten(text) or text)
    return None


def _parse_fused_scripts(chars: str) -> tuple[str, str, str, str]:
    """Split a fused ``_0^1 f`` script into (sub, sup, trailing, dangling).

    Each bound is a single token (``0``, ``1``) or a ``{…}`` group; anything
    after the scripts (`` f``) is trailing content glued back beside the box.
    A trailing ``_``/``^`` with no token (``_0^`` before ``\\infty``) is returned
    as *dangling* so the caller can bind the following node as that script.
    """
    sub = sup = dangling = ""
    pos = 0
    n = len(chars)
    while pos < n and chars[pos] in ("_", "^"):
        mark = chars[pos]
        pos += 1
        if pos >= n:  # mark with no token → bound is the next node
            dangling = mark
            break
        if pos < n and chars[pos] == "{":
            depth = 1
            start = pos + 1
            pos += 1
            while pos < n and depth:
                if chars[pos] == "{":
                    depth += 1
                elif chars[pos] == "}":
                    depth -= 1
                pos += 1
            token = chars[start : pos - 1]
        else:
            start = pos
            pos += 1
            token = chars[start:pos]
        if mark == "_":
            sub = token
        else:
            sup = token
    return sub, sup, chars[pos:], dangling


def _walk_nodes(nodes: list, *, display: bool) -> MathBox:
    """Turn a nodelist into a MathBox, recursing into ``\\frac`` / matrix envs.

    Consecutive "plain" nodes (anything that is not a fraction or matrix) are
    coalesced and flattened to a single Unicode atom via :func:`flatten`; the
    structural constructs become 2D boxes (or their single-line forms when
    ``display`` is false). The pieces are then glued on their baselines.
    """
    pieces: list[MathBox] = []
    pending: list = []

    def flush() -> None:
        if pending:
            raw = _flat_nodes(pending)
            core = flatten(raw)
            if core is None:  # pylatexenc unavailable / parse failed
                text = raw
            elif not core:
                # The run flattened to nothing (a lone spacing node between two
                # boxes) — collapse to one separating space when a piece precedes.
                text = " " if (pieces and raw.strip() == "") else ""
            else:
                # ``flatten`` collapses boundary whitespace; when a box already
                # precedes this run, restore the single space that separated them
                # so they don't abut (``∫(-∞)^(+∞)e⁻ˣ²`` → ``∫(-∞)^(+∞) e⁻ˣ²``).
                text = core
                if pieces and raw[:1].isspace():
                    text = " " + text
            pieces.append(atom(text))
            pending.clear()

    i = 0
    while i < len(nodes):
        n = nodes[i]
        if isinstance(n, LatexMacroNode) and n.macroname in _BIG_OPS:
            consumed, box = _big_operator(nodes, i, display=display)
            if box is not None:
                flush()
                pieces.append(box)
                i = consumed
                continue
        if isinstance(n, LatexMacroNode) and n.macroname == "frac" and n.nodeargd:
            args = n.nodeargd.argnlist
            if len(args) >= 2 and args[0] is not None and args[1] is not None:
                flush()
                num = _walk_nodes(_group_nodelist(args[0]), display=display)
                den = _walk_nodes(_group_nodelist(args[1]), display=display)
                pieces.append(frac(num, den) if display else frac_inline(num, den))
                i += 1
                continue
        if isinstance(n, LatexMacroNode) and n.macroname == "sqrt" and n.nodeargd:
            args = n.nodeargd.argnlist
            # \sqrt{x} → args=[None, {x}]; \sqrt[n]{x} → args=[[n], {x}].
            if args and args[-1] is not None:
                flush()
                radicand = _walk_nodes(_group_nodelist(args[-1]), display=display)
                index_str = ""
                if len(args) >= 2 and args[0] is not None:
                    index_str = flatten(_flat_nodes(_group_nodelist(args[0]))) or ""
                pieces.append(sqrt(radicand, index_str) if display else sqrt_inline(radicand, index_str))
                i += 1
                continue
        if isinstance(n, LatexEnvironmentNode) and n.environmentname in _MATRIX_DELIMS:
            flush()
            left, right = _MATRIX_DELIMS[n.environmentname]
            cells = [[_walk_nodes(cell, display=display) for cell in row] for row in _split_matrix(n)]
            pieces.append(matrix(cells, left, right) if display else matrix_inline(cells, left, right))
            i += 1
            continue
        pending.append(n)
        i += 1
    flush()
    return hconcat(pieces) if pieces else atom("")


def build_box(latex: str, *, display: bool) -> MathBox | None:
    """Layout LaTeX math *latex* as a :class:`MathBox`, or ``None`` on failure.

    ``display=False`` yields a height-1 box (inline form); ``display=True`` lets
    fractions/matrices stack in 2D. Returns ``None`` when pylatexenc is missing or
    the fragment can't be parsed so the caller can fall back to the raw source.
    """
    if LatexWalker is None:
        return None
    try:
        nodes, _, _ = LatexWalker(latex).get_latex_nodes()
        return _walk_nodes(list(nodes), display=display)
    except Exception:
        return None


__all__ = [
    "MathBox",
    "atom",
    "hconcat",
    "frac",
    "sqrt",
    "matrix",
    "build_box",
    "flatten",
]
