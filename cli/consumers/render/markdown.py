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

Every rich host (terminal :class:`TerminalConsumer`, Textual ``AssistantBlock``,
and any future web host reusing these builders) should render assistant markdown
via :func:`themed_markdown` so the "look" stays defined in exactly one place.
"""

from __future__ import annotations

import re

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import BlockQuote, Heading, Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

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


def linkify_markdown(markup: str) -> str:
    """Wrap bare ``http(s)://`` URLs in *markup* as ``<url>`` CommonMark autolinks.

    Markdown-syntax links (``[t](u)`` / ``<u>``) and any code (fenced or inline)
    are left byte-for-byte untouched; only URLs sitting in plain prose become
    clickable autolinks so rich renders them with an OSC 8 hyperlink.
    """
    out: list[str] = []
    idx = 0
    for skip in _SKIP_RE.finditer(markup):
        out.append(_BARE_URL_RE.sub(lambda m: f"<{m.group()}>", markup[idx : skip.start()]))
        out.append(skip.group())  # verbatim — never linkify inside code/links
        idx = skip.end()
    out.append(_BARE_URL_RE.sub(lambda m: f"<{m.group()}>", markup[idx:]))
    return "".join(out)


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


# claude-code prefixes each blockquote line with a thin ``▎`` (U+258E, left
# one-quarter block) bar and italicises the quoted text. rich's own ``BlockQuote``
# already draws a left bar, but with the heavier ``▌`` (half block) and no italic —
# so we subclass it to match claude-code's lighter, italic look.
BLOCKQUOTE_BAR = "\u258e"  # ▎


class BrandBlockQuote(BlockQuote):
    """A :class:`~rich.markdown.BlockQuote` with claude-code's thin ``▎`` bar + italic.

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


class BrandMarkdown(Markdown):
    """:class:`~rich.markdown.Markdown` with brand element renderers swapped in.

    ``elements`` is rich's documented registry (markdown-token → render class);
    copying it and rebinding ``heading_open`` (drop the h1 box) and
    ``blockquote_open`` (thin ``▎`` bar + italic) is the supported way to customise
    single elements without touching rich internals.
    """

    elements = {
        **Markdown.elements,
        "heading_open": BrandHeading,
        "blockquote_open": BrandBlockQuote,
    }


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
