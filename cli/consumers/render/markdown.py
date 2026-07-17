#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Brand-themed Markdown renderable — host-independent, self-styling.

``rich.markdown.Markdown`` is the right engine (CommonMark parsing, tables, code
fences, nested lists) but two things fight our look:

1. **Colours** come from ``markdown.*`` style names resolved on the *active*
   console at render time. Textual renders our renderables through the running
   app's console — not one we construct — so simply handing rich a ``Console``
   built with our theme would style the terminal stream yet leave the Textual app
   on rich's defaults. The two hosts would drift.
2. **h1 draws a heavy box** (``Heading.__rich_console__`` hard-codes
   ``Panel(box=box.HEAVY)``); no style token can remove it.

Both are solved without monkeypatching rich:

* :func:`themed_markdown` returns a renderable that pushes :data:`MARKDOWN_THEME`
  onto *whichever* console is rendering it (via ``console.use_theme`` inside its
  own ``__rich_console__``). The theme travels with the content, so a terminal
  ``Console`` and Textual's app console produce identical output.
* :class:`BrandHeading` subclasses rich's ``Heading`` to drop the h1 box, and is
  registered through rich's public ``Markdown.elements`` extension point (a
  ``BrandMarkdown`` subclass), so we ride rich's own mechanism rather than
  patching it.

LaTeX math (``$…$`` / ``$$…$$`` / ``\\(…\\)`` / ``\\[…\\]``) is flattened to Unicode
and boxed: a terminal can't typeset math, so ``x^2`` becomes ``x²`` and ``\\alpha``
becomes ``α``, painted on the ``markdown.math`` background so the formula reads as
a distinct span. This rides a markdown-it inline rule inside :class:`BrandMarkdown`
(:func:`_math_inline_rule`), so math inside code is skipped for free.

Every rich host (terminal :class:`TerminalConsumer`, Textual ``AssistantBlock``,
and any future web host reusing these builders) should render assistant markdown
via :func:`themed_markdown` so the "look" stays defined in exactly one place.
"""

from __future__ import annotations

import re
from typing import Callable

from markdown_it import MarkdownIt
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import BlockQuote, Heading, Markdown, MarkdownElement
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from mote.cli.consumers.render.mathbox import build_box
from mote.cli.consumers.render.theme import MARKDOWN_THEME

# Bare http(s) URL, used to autolink URLs typed as plain prose. CommonMark only
# linkifies ``[text](url)`` / ``<url>`` — a bare URL stays literal — so we wrap
# bare ones as ``<url>`` autolinks before parsing. Segments that must stay
# literal (fenced/inline code, already-linked spans) are masked out first.
_BARE_URL_RE = re.compile(r"(?<![\(<\"'`])https?://[^\s<>\"'`\)\]]+")
# Spans to leave untouched: fenced code blocks, inline code, markdown links, and
# existing autolinks — matched in priority order and re-emitted verbatim.
_SKIP_RE = re.compile(
    r"```.*?```"  # fenced code block
    r"|`[^`]*`"  # inline code
    r"|\[[^\]]*\]\([^\)]*\)"  # [text](url) link
    r"|<https?://[^>]*>",  # <url> autolink
    re.DOTALL,
)


def _map_outside_skips(markup: str, transform: Callable[[str], str]) -> str:
    """Apply *transform* to the plain-prose spans of *markup*, verbatim inside code.

    The "operate outside code/links" seam used by :func:`linkify_markdown` —
    fenced/inline code, markdown links and existing autolinks (:data:`_SKIP_RE`)
    are re-emitted byte-for-byte, so the pass never rewrites a URL that lives
    inside code. (Math is handled later, at parse time, not by this string pass.)
    """
    out: list[str] = []
    idx = 0
    for skip in _SKIP_RE.finditer(markup):
        out.append(transform(markup[idx : skip.start()]))
        out.append(skip.group())  # verbatim — never rewrite inside code/links
        idx = skip.end()
    out.append(transform(markup[idx:]))
    return "".join(out)


def linkify_markdown(markup: str) -> str:
    """Wrap bare ``http(s)://`` URLs in *markup* as ``<url>`` CommonMark autolinks.

    Markdown-syntax links (``[t](u)`` / ``<u>``) and any code (fenced or inline)
    are left byte-for-byte untouched; only URLs sitting in plain prose become
    clickable autolinks so rich renders them with an OSC 8 hyperlink.
    """
    return _map_outside_skips(markup, lambda seg: _BARE_URL_RE.sub(lambda m: f"<{m.group()}>", seg))


# --- LaTeX math → boxed Unicode (inline) / 2D layout (display) --------------
#
# A terminal can't typeset math, so LaTeX spans are rendered by the box engine in
# :mod:`mote.cli.consumers.render.mathbox`, split by *dimensionality*:
#
#   * INLINE ``$…$`` / ``\(…\)`` — physically one line tall, so it is flattened to
#     a single Unicode line (``x^2`` → ``x²``, ``\frac{a+b}{c-d}`` → ``(a+b)/(c-d)``)
#     and painted on the ``markdown.math`` background, reading as a boxed span.
#   * DISPLAY ``$$…$$`` / ``\[…\]`` — its own block, so it lays out in 2D (a real
#     fraction bar, stacked matrix rows inside stretchy brackets) via a ``MathBlock``
#     element.
#
# Detection rides a markdown-it inline rule (see :func:`_math_inline_rule`) at
# PARSE time — the span becomes a proper token rich styles/renders, and math
# inside inline/fenced code is skipped for free (those tokens consume their ``$``
# before the rule runs). Both hosts (terminal + Textual) share :class:`BrandMarkdown`.

# Math delimiters, anchored so ``pattern.match(src, pos)`` fires at a candidate
# ``$``/``\`` position. Display forms (``$$…$$`` / ``\[…\]``) are tried before
# inline (``$…$`` / ``\(…\)``) so ``$$`` isn't mistaken for an empty ``$…$``.
# The inline-dollar form is deliberately strict so plain prose isn't captured:
#   * no space just inside the delimiters — "$5 and $10" stays currency;
#   * a backtick can't sit in the body — an unmatched ``$`` in prose can't reach a
#     ``$`` living inside a later code span (the rule scans raw source, before
#     code tokens are formed, so this guard is what keeps code math verbatim);
#   * the closing ``$`` can't be followed by a word char — so "$10.Code $x" won't
#     latch onto the ``$`` in ``$x``.
_MATH_DISPLAY_DOLLAR = re.compile(r"\$\$([^`]+?)\$\$", re.DOTALL)
_MATH_INLINE_DOLLAR = re.compile(r"\$(?!\s)((?:\\.|[^$\n`])+?)(?<!\s)\$(?![\w$])")
_MATH_DISPLAY_BRACKET = re.compile(r"\\\[([^`]+?)\\\]", re.DOTALL)
_MATH_INLINE_PAREN = re.compile(r"\\\(([^`]+?)\\\)", re.DOTALL)


def _math_inline_rule(state, silent: bool) -> bool:
    """markdown-it inline rule: turn a LaTeX span into a ``math`` / ``math_block`` token.

    Fires at a ``$`` or ``\\`` position (both are inline terminators, so the plain
    ``text`` rule yields to us there). A DISPLAY form (``$$…$$`` / ``\\[…\\]``) pushes
    a self-closing ``math_block`` token carrying the RAW LaTeX for the 2D
    :class:`MathBlock` renderer; an INLINE form (``$…$`` / ``\\(…\\)``) is flattened
    to one Unicode line and pushed as a ``math`` token rich paints with the
    ``markdown.math`` background. A span that can't be parsed (or currency like
    "$5") fails the rule, so the ``$`` falls through to ordinary text untouched.
    """
    src = state.src
    pos = state.pos
    ch = src[pos]
    if ch == "$":
        display = _MATH_DISPLAY_DOLLAR.match(src, pos)
        inline = None if display else _MATH_INLINE_DOLLAR.match(src, pos)
    elif ch == "\\":
        display = _MATH_DISPLAY_BRACKET.match(src, pos)
        inline = None if display else _MATH_INLINE_PAREN.match(src, pos)
    else:
        return False
    match = display or inline
    if match is None:
        return False
    latex = match.group(1).strip()
    if display is not None:
        # Keep the raw LaTeX; MathBlock lays it out in 2D. Validate it parses now
        # so an unmatched ``$$`` can't swallow prose (mirrors the inline guard).
        if build_box(latex, display=True) is None:
            return False
        if not silent:
            # tag must stay OUT of ``inlines`` so rich routes it to the
            # ``math_block`` element (2D layout) rather than the inline-style path.
            token = state.push("math_block", "math_block", 0)
            token.content = latex
            token.markup = match.group(0)
        state.pos = match.end()
        return True
    box = build_box(latex, display=False)
    unicode_text = box.to_line() if box is not None else None
    if not unicode_text:
        return False
    if not silent:
        token = state.push("math", "math", 0)
        token.content = unicode_text
        token.markup = match.group(0)
    state.pos = match.end()
    return True


class BrandHeading(Heading):
    """A :class:`~rich.markdown.Heading` that renders h1 as plain accent text.

    rich boxes h1 in a heavy-ruled :class:`~rich.panel.Panel`; that frame clashes
    with our light bullet/branch chrome. We keep rich's centring and per-tag style
    resolution but emit the heading as bare styled text for *every* level, so an
    h1 reads as bold brand-orange text rather than a boxed banner.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text
        text.justify = "left"
        if self.tag == "h2":
            # Preserve rich's blank spacer above an h2 for visual separation.
            yield Text("")
        yield text


# The reference look prefixes each blockquote line with a thin ``▎`` (U+258E, left
# one-quarter block) bar and italicises the quoted text. rich's own ``BlockQuote``
# already draws a left bar, but with the heavier ``▌`` (half block) and no italic —
# so we subclass it to match that lighter, italic look.
BLOCKQUOTE_BAR = "\u258e"  # ▎


class BrandBlockQuote(BlockQuote):
    """A :class:`~rich.markdown.BlockQuote` with a thin ``▎`` bar + italic.

    rich renders a blockquote as its children prefixed by a ``▌ `` half-block bar;
    we keep that structure (render children to lines, prefix each) but swap in the
    thinner ``▎`` and italicise the quoted body so a quote reads distinctly without
    the heavier default bar. The bar keeps the dim ``markdown.block_quote`` style
    (themed to :data:`Palette.DIM`); the text is the same style plus italic.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(width=options.max_width - 2)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        bar = Segment(BLOCKQUOTE_BAR + " ", self.style)
        italic = self.style + Style(italic=True)
        new_line = Segment("\n")
        for line in lines:
            yield bar
            yield from Segment.apply_style(line, italic)
            yield new_line


class MathBlock(MarkdownElement):
    """A display-math ``$$…$$`` / ``\\[…\\]`` span laid out in 2D on the terminal.

    The self-closing ``math_block`` token carries the raw LaTeX; we hand it to the
    box engine (:func:`~mote.cli.consumers.render.mathbox.build_box`) which returns
    a rectangle of rows (fraction bar, stacked matrix rows inside stretchy
    brackets). Each row is emitted as ``Text`` on the ``markdown.math`` background,
    padded to a clean rectangle so the formula reads as one boxed block. A fragment
    the engine can't lay out degrades to its single-line form, never the raw source.
    """

    @classmethod
    def create(cls, markdown: Markdown, token) -> "MathBlock":
        return cls(token.content)

    def __init__(self, latex: str) -> None:
        self.latex = latex

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        style = console.get_style("markdown.math", default="none")
        box = build_box(self.latex, display=True)
        if box is None:
            yield Text(self.latex, style=style)
            return
        for line in box.render_lines():
            yield Text(line, style=style)


class BrandMarkdown(Markdown):
    """:class:`~rich.markdown.Markdown` with brand element renderers + math layout.

    ``elements`` is rich's documented registry (markdown-token → render class);
    copying it and rebinding ``heading_open`` (drop the h1 box), ``blockquote_open``
    (thin ``▎`` bar + italic) and ``math_block`` (2D formula) is the supported way
    to customise single elements without touching rich internals.

    Math support rides two of rich's own extension points, no monkeypatching:
    :attr:`inlines` gains ``math`` so a self-closing ``math`` token is painted with
    the ``markdown.math`` style (the inline background box), while a ``math_block``
    token routes to :class:`MathBlock` for 2D layout. :meth:`__init__` re-parses the
    source through a parser carrying :func:`_math_inline_rule` (rich builds its
    parser inline, so the rule can only be injected by re-running ``parse``).
    """

    #: rich styles any self-closing inline token whose tag is here via
    #: ``markdown.{tag}`` — adding ``math`` is what gives inline formulas their box.
    inlines = Markdown.inlines | {"math"}

    elements = {
        **Markdown.elements,
        "heading_open": BrandHeading,
        "blockquote_open": BrandBlockQuote,
        "math_block": MathBlock,
    }

    def __init__(self, markup: str, *args, **kwargs) -> None:
        super().__init__(markup, *args, **kwargs)
        # rich's __init__ already parsed with a vanilla parser; re-parse through
        # one carrying the math rule (mirrors rich's own strikethrough/table
        # enables) so ``$…$`` spans become ``math`` tokens.
        parser = MarkdownIt().enable("strikethrough").enable("table")
        parser.inline.ruler.before("text", "math", _math_inline_rule)
        self.parsed = parser.parse(self.markup)


class _ThemedMarkdown:
    """A renderable that applies :data:`MARKDOWN_THEME` to the rendering console.

    Carrying the theme in the renderable (rather than on a fixed console) is what
    makes the same object render identically under the terminal stream and the
    Textual app — each host renders through its own console, and we push our theme
    onto whichever one asks for our lines.
    """

    __slots__ = ("_markup",)

    def __init__(self, markup: str) -> None:
        # Autolink bare URLs up front so both hosts render them as OSC 8 links.
        # (LaTeX math is flattened + boxed later, at parse time, by BrandMarkdown.)
        self._markup = linkify_markdown(markup)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        with console.use_theme(MARKDOWN_THEME):
            yield BrandMarkdown(self._markup)


def themed_markdown(markup: str):
    """Return a brand-themed, host-independent Markdown renderable for ``markup``."""
    return _ThemedMarkdown(markup)


__all__ = [
    "themed_markdown",
    "linkify_markdown",
    "BrandHeading",
    "BrandBlockQuote",
    "BrandMarkdown",
    "BLOCKQUOTE_BAR",
]
