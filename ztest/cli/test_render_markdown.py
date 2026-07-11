#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Brand-themed Markdown — the host-independent, self-styling renderable.

``themed_markdown`` re-skins rich's Markdown so it reads like the rest of the
transcript (brand-orange headings/bullets, dim chrome) instead of rich's default
clashing palette, and drops the heavy h1 box. The two properties that matter most
are covered here: the theme is applied to *whichever* console renders the object
(so the terminal stream and the Textual app can't drift), and the h1 box is gone.

We render to a truecolor ``StringIO`` console and assert on the emitted SGR codes:
a brand-orange span is ``38;2;215;119;87`` (``Palette.BRAND`` = #d77757).
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("rich")

from mote.cli.consumers.render.markdown import BrandMarkdown, themed_markdown
from mote.cli.consumers.render.palette import Palette
from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

# ``Palette.BRAND`` (#d77757) emitted as a truecolor foreground SGR.
_BRAND_SGR = "38;2;215;119;87"


def _render(markup: str, *, width: int = 60, theme: Theme | None = None) -> str:
    console = Console(
        file=io.StringIO(),
        force_terminal=True,
        width=width,
        color_system="truecolor",
        theme=theme,
    )
    console.print(themed_markdown(markup))
    return console.file.getvalue()


def test_h1_is_brand_orange_without_the_heavy_box():
    # rich's default h1 draws a HEAVY-ruled panel; ours is bare accent text.
    out = _render("# Heading One")
    assert _BRAND_SGR in out  # brand-orange heading
    assert "\u250f" not in out and "\u2503" not in out  # no ┏ / ┃ box glyphs


def test_default_markdown_h1_still_boxes_as_a_control():
    # Guards the regression: unthemed rich Markdown DOES box the h1, proving our
    # BrandHeading is what removes it (not some environment quirk).
    console = Console(file=io.StringIO(), force_terminal=True, width=60)
    console.print(Markdown("# Heading One"))
    assert "\u250f" in console.file.getvalue()  # ┏ — rich's default h1 box


def test_list_bullets_use_the_brand_accent():
    out = _render("- first\n- second")
    assert _BRAND_SGR in out


def test_inline_code_drops_richs_cyan_on_black_for_brand_fg():
    out = _render("use the `foo()` call")
    code_line = next(line for line in out.splitlines() if "foo" in line)
    assert _BRAND_SGR in code_line  # brand foreground on the inline code span


def test_block_quote_is_dim_not_richs_magenta():
    # rich defaults block quotes to magenta; ours recede to the dim tone. We
    # assert the loud magenta default (``38;5;5``/``ff00ff``) is absent.
    out = _render("> a quoted line")
    assert "38;5;5" not in out and "255;0;255" not in out


def test_block_quote_uses_thin_bar_and_italic_not_richs_half_block():
    # claude-code marks a quote with the thin ``▎`` bar + italic text, not rich's
    # heavier ``▌`` half-block. The bar is present, the half-block is gone, and the
    # quoted body carries the italic SGR (``3``).
    out = _render("> a quoted line")
    assert "\u258e" in out  # ▎ left one-quarter block bar
    assert "\u258c" not in out  # ▌ (rich's default) replaced
    assert "\x1b[3;" in out  # italic SGR (3) leads the quoted-text span


def test_heading_hierarchy_differs_between_h1_and_h2():
    # h1 (brand) and h2 (a distinct colour) must not render with the same SGR, so
    # nested outlines stay legible — the whole point of a per-level theme.
    h1 = _render("# Title")
    h2 = _render("## Subtitle")
    assert _BRAND_SGR in h1
    assert _BRAND_SGR not in h2  # h2 steps to a different colour


def test_theme_travels_with_the_renderable_across_hosts():
    """The renderable styles ITSELF, independent of the host console's own theme.

    This is the architectural guarantee: Textual renders our renderables through
    the running app's console (not one we build), so a fixed themed ``Console``
    would style the terminal yet leave Textual on rich's defaults. Because
    ``themed_markdown`` pushes its theme onto whichever console renders it, the
    output is identical whether the host console is bare or carries a hostile
    conflicting theme.
    """
    bare = _render("# Title")
    hostile = _render(
        "# Title",
        theme=Theme({"markdown.h1": "bold #00ff00"}),  # a clashing host theme
    )
    assert bare == hostile
    assert _BRAND_SGR in bare  # and it's OUR brand, not the host's green


def test_brand_markdown_registers_the_brand_heading():
    # The heading element is swapped via rich's public ``elements`` registry, not
    # a monkeypatch, so the base ``Markdown`` class stays untouched.
    from mote.cli.consumers.render.markdown import BrandHeading

    assert BrandMarkdown.elements["heading_open"] is BrandHeading
    assert Markdown.elements["heading_open"] is not BrandHeading  # base untouched


# --- bare-URL autolinking (linkify_markdown) -------------------------------

# OSC 8 hyperlink introducer — rich emits ``\x1b]8;;URL...`` for any link span.
_OSC8 = "\x1b]8;;"


def test_linkify_markdown_wraps_a_bare_url_as_an_autolink():
    from mote.cli.consumers.render.markdown import linkify_markdown

    out = linkify_markdown("see https://example.com for more")
    assert "<https://example.com>" in out


def test_linkify_markdown_leaves_existing_markdown_link_untouched():
    from mote.cli.consumers.render.markdown import linkify_markdown

    src = "click [here](https://example.com) now"
    # The inline URL inside a ``[t](u)`` link must not get double-wrapped.
    assert linkify_markdown(src) == src


def test_linkify_markdown_leaves_existing_autolink_untouched():
    from mote.cli.consumers.render.markdown import linkify_markdown

    src = "at <https://example.com> already"
    assert linkify_markdown(src) == src


def test_linkify_markdown_skips_urls_inside_inline_code():
    from mote.cli.consumers.render.markdown import linkify_markdown

    src = "run `curl https://example.com` here"
    assert linkify_markdown(src) == src


def test_linkify_markdown_skips_urls_inside_fenced_code():
    from mote.cli.consumers.render.markdown import linkify_markdown

    src = "```\ncurl https://example.com\n```"
    assert linkify_markdown(src) == src


def test_bare_url_renders_as_an_osc8_hyperlink():
    # End-to-end: a bare URL in prose becomes a clickable OSC 8 link after our
    # autolink pass (rich alone leaves a bare URL literal).
    out = _render("see https://example.com now")
    assert _OSC8 in out
    assert "https://example.com" in out
