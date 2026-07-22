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

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

from mote.cli.consumers.render.markdown import BrandMarkdown, themed_markdown
from mote.cli.consumers.render.palette import Palette

# ``Palette.BRAND`` (#d77757) emitted as a truecolor foreground SGR.
_BRAND_SGR = "38;2;215;119;87"


def _render(markup: str, *, width: int = 60, theme: Theme | None = None) -> str:
    console = Console(
        file=io.StringIO(),
        force_terminal=True,
        no_color=False,
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
    # The renderer marks a quote with the thin ``▎`` bar + italic text, not rich's
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


# --- LaTeX math → boxed Unicode span ---------------------------------------

# ``Palette.MATH_BG`` (#33251f) emitted as a truecolor BACKGROUND SGR — its
# presence proves the formula span carries the ``markdown.math`` box.
_MATH_BG_SGR = "48;2;51;37;31"


def test_flatten_preserves_script_grouping():
    # The pure single-line helper: ^{…}/_{…} grouping survives (2¹⁰, not the lossy
    # 2¹0) and macros expand to their symbols.
    from mote.cli.consumers.render.mathbox import flatten

    assert flatten("2^{10}") == "2¹⁰"
    assert flatten(r"\sum_{i=1}^{n} i") == "∑ᵢ₌₁ⁿ i"
    assert flatten(r"\frac{a}{b}") == "a/b"


def test_inline_math_is_flattened_and_boxed():
    # End-to-end: ``$…$`` becomes the Unicode glyph, the ``$`` delimiter is gone,
    # and the span carries the distinct math background.
    out = _render("energy $E = mc^2$ today")
    assert "mc²" in out
    assert "$" not in out
    assert _MATH_BG_SGR in out  # the boxed background


def test_display_math_dollars_and_bracket_are_boxed():
    for src in ("$$x^2 + y^2$$", r"\[ a_{ij} \]"):
        out = _render(src)
        assert _MATH_BG_SGR in out
    assert "x²" in _render("$$x^2 + y^2$$")
    assert "aᵢⱼ" in _render(r"\[ a_{ij} \]")


def test_inline_paren_math_is_boxed():
    out = _render(r"see \(\frac{a}{b}\) here")
    assert "a/b" in out
    assert _MATH_BG_SGR in out


def test_currency_is_not_treated_as_math():
    # No space-hugging delimiters, so "$5 and $10" is prose — the ``$`` stays and
    # no math box is drawn.
    out = _render("it costs $5 and $10 total")
    assert "$5" in out and "$10" in out
    assert _MATH_BG_SGR not in out


def test_math_inside_code_is_left_verbatim():
    # ``$…$`` inside inline code is consumed as a code span before the math rule
    # sees the ``$``, so it stays literal (no box, no flattening).
    out = _render("run `$x^2$` verbatim")
    assert "$x^2$" in out
    assert _MATH_BG_SGR not in out


def test_stray_currency_does_not_reach_into_a_later_code_span():
    # Regression: a currency ``$`` in prose must not latch onto a ``$`` living
    # inside a following inline-code span (the rule scans raw source). "$10" stays
    # prose and the code renders verbatim, so no math box appears at all.
    out = _render("cost $5 and $10. Code stays literal: `$x^2$`.")
    assert "$5" in out and "$10" in out
    assert "$x^2$" in out
    assert _MATH_BG_SGR not in out


# --- Inline math: fractions/matrices flattened unambiguously ----------------


def test_inline_fraction_is_parenthesised_when_compound():
    # A compound numerator/denominator gets parens so ``(a+b)/(c-d)`` can't read
    # as the ambiguous ``a+b/c-d``. Simple atoms stay bare (``x/y``).
    out = _render(r"the ratio $\frac{a+b}{c-d}$ here")
    assert "(a+b)/(c-d)" in out
    assert _MATH_BG_SGR in out


def test_inline_matrix_uses_semicolon_row_notation():
    out = _render(r"see $\begin{bmatrix} a & b \\ c & d \end{bmatrix}$ inline")
    assert "[ a b ; c d ]" in out


# --- Display math: 2D box layout --------------------------------------------


def test_display_fraction_stacks_over_a_bar():
    # ``$$\frac{a}{b}$$`` lays out on three rows: numerator, ─── rule, denominator.
    out = _render(r"$$\frac{a+b}{c-d}$$")
    assert "───" in out  # the fraction bar
    assert "a+b" in out and "c-d" in out
    assert _MATH_BG_SGR in out  # the block is boxed


def test_inline_sqrt_uses_radical_glyph():
    # ``\sqrt{x}`` inline flattens to ``√(…)`` (parenthesised when compound).
    out = _render(r"the root $\sqrt{b^2-4ac}$ here")
    assert "\u221a(b²-4ac)" in out  # √(b²-4ac)
    assert _MATH_BG_SGR in out


def test_inline_sqrt_index_is_superscripted():
    out = _render(r"see $\sqrt[3]{x+1}$ inline")
    assert "³\u221a(x+1)" in out  # ³√(x+1)


def test_display_sqrt_extends_an_overline_vinculum():
    # ``$$\sqrt{…}$$`` lays out with a ``───`` overline over the radicand and the
    # ``√`` checkmark on the baseline row — the 2D radical.
    out = _render(r"$$\sqrt{b^2-4ac}$$")
    assert "\u221a" in out  # √ radical sign
    assert "\u2500" in out  # ─ overline vinculum
    assert "b²-4ac" in out
    assert _MATH_BG_SGR in out


def test_display_matrix_uses_stretchy_brackets():
    out = _render(r"$$\begin{pmatrix} a & b \\ c & d \end{pmatrix}$$")
    # Stretchy round-bracket glyphs across the stacked rows.
    assert "⎛" in out and "⎝" in out
    assert "⎞" in out and "⎠" in out


# --- Scripts: nested exponents & derivative primes --------------------------


def test_flatten_maps_nested_exponent():
    # ``e^{x^2}`` used to degrade to ``e^x^2`` (the inner ``^`` aborted the run);
    # the nested script is converted first so it maps fully to ``eˣ²``.
    from mote.cli.consumers.render.mathbox import flatten

    assert flatten("e^{x^2}") == "eˣ²"
    assert flatten("e^{-x^2}") == "e⁻ˣ²"
    assert flatten("x^{2n}") == "x²ⁿ"


def test_flatten_parenthesises_unmappable_compound_exponent():
    # Euler's identity: ``π`` has no superscript glyph, so ``e^{i\pi}`` can't map
    # fully — it parenthesises as ``e^(iπ)`` rather than leaking a bare ``e^iπ``.
    from mote.cli.consumers.render.mathbox import flatten

    assert flatten(r"e^{i\pi} + 1 = 0") == "e^(iπ) + 1 = 0"
    assert flatten(r"e^{i\theta}") == "e^(iθ)"
    # A LONE unmappable symbol stays bare — parens there would only add noise.
    assert flatten("x^q") == "x^q"


def test_flatten_maps_derivative_primes():
    # ``''`` used to mangle into a curly quote (``”``); real prime glyphs now.
    from mote.cli.consumers.render.mathbox import flatten

    assert flatten("f'(x)") == "f′(x)"
    assert flatten("f''(x)") == "f″(x)"
    assert flatten("f'''(x)") == "f‴(x)"


# --- Big operators: limit positioning ---------------------------------------


def test_display_sum_stacks_limits_over_the_symbol():
    # ``\sum_{i=1}^{n}`` in display math puts ``n`` above and ``i=1`` below ``∑``.
    from mote.cli.consumers.render.mathbox import build_box

    box = build_box(r"\sum_{i=1}^{n} i", display=True)
    lines = box.render_lines()
    assert box.height == 3
    assert "n" in lines[0]  # upper limit on top
    assert "∑" in lines[1]  # operator on the middle (baseline) row
    assert "i=1" in lines[2]  # lower limit on the bottom


def test_display_integral_sets_limits_beside_a_tall_sign():
    # ``\int_0^1`` draws a stretched sign (``⌠⎮⌡``) carrying its bounds at the
    # top-/bottom-right — the conventional display-integral layout.
    from mote.cli.consumers.render.mathbox import build_box

    box = build_box(r"\int_0^1 f\,dx", display=True)
    lines = box.render_lines()
    assert box.height == 3
    assert lines[0].startswith("⌠") and lines[-1].startswith("⌡")  # tall sign
    assert "1" in lines[0] and "0" in lines[-1]  # bounds beside the sign


def test_display_integral_macro_bound_is_captured():
    # A macro upper bound (``\infty``) after a fused ``_0^`` is bound as the sup.
    from mote.cli.consumers.render.mathbox import build_box

    box = build_box(r"\int_0^\infty x^2\,dx", display=True)
    lines = box.render_lines()
    assert "∞" in lines[0]  # upper bound present, not dropped
    assert "0" in lines[-1]


def test_display_lim_subscript_is_placed_under_the_word():
    # ``\lim_{x \to 0}`` stacks the bound under ``lim`` instead of leaking ``_``.
    from mote.cli.consumers.render.mathbox import build_box

    box = build_box(r"\lim_{x \to 0} f(x)", display=True)
    lines = box.render_lines()
    assert any("lim" in line for line in lines)
    assert any("→" in line for line in lines)
    assert not any("_" in line for line in lines)  # no raw underscore leak


def test_inline_lim_parenthesises_unmappable_bound():
    # Inline, ``x→0`` has no subscript glyphs, so the bound is parenthesised and
    # attached (``lim(x→0)``) rather than leaking ``lim_x →0``.
    from mote.cli.consumers.render.mathbox import build_box

    line = build_box(r"\lim_{x \to 0} f(x)", display=False).to_line()
    assert "lim(" in line and "_" not in line


def test_inline_sum_keeps_unicode_scripts():
    # Fully-mappable scripts stay as compact Unicode subscripts inline.
    from mote.cli.consumers.render.mathbox import build_box

    line = build_box(r"\sum_{i=1}^{n} i", display=False).to_line()
    assert "∑ᵢ₌₁ⁿ" in line


def test_inline_improper_integral_marks_both_bounds_and_keeps_the_gap():
    # ``\int_{-\infty}^{+\infty}`` has unmappable bounds, so both are
    # parenthesised SYMMETRICALLY (``_(…)^(…)`` — not a bare sub with a lone
    # ``^`` sup), and the space before the integrand survives the box glue so it
    # doesn't abut the operator (``…^(+∞) e⁻ˣ²`` not ``…^(+∞)e⁻ˣ²``).
    from mote.cli.consumers.render.mathbox import build_box

    line = build_box(r"\int_{-\infty}^{+\infty} e^{-x^2}\,dx", display=False).to_line()
    assert "∫_(-∞)^(+∞)" in line  # symmetric sub/sup markers
    assert "^(+∞) e⁻ˣ²" in line  # separating space preserved before integrand


# --- MathBox box algebra (unit) ---------------------------------------------


def test_mathbox_frac_puts_baseline_on_the_bar():
    from mote.cli.consumers.render.mathbox import atom, frac

    box = frac(atom("1"), atom("i²"))
    assert box.lines[box.baseline].startswith("─")  # baseline row is the rule
    assert box.height == 3


def test_mathbox_hconcat_aligns_on_baseline():
    from mote.cli.consumers.render.mathbox import atom, frac, hconcat

    # gluing a tall fraction to a plain atom lifts the atom onto the shared axis.
    box = hconcat([atom("x="), frac(atom("1"), atom("2"))])
    assert box.height == 3
    assert box.lines[box.baseline].startswith("x=")


def test_mathbox_sqrt_puts_checkmark_on_baseline_under_a_vinculum():
    from mote.cli.consumers.render.mathbox import atom, sqrt

    box = sqrt(atom("x+1"))
    # Top row is the overline; the radicand row (baseline) carries the √.
    assert box.lines[0].strip().startswith("─")  # vinculum on top
    assert "\u221a" in box.lines[box.baseline]  # √ on the baseline row
    assert box.height == 2


def test_build_box_falls_back_gracefully_on_unknown():
    from mote.cli.consumers.render.mathbox import build_box

    # An unmodelled construct still yields a (flat) box, never None/raw soup.
    box = build_box(r"\alpha + \beta", display=True)
    assert box is not None
    assert box.height == 1
