#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Console-free rich *builders* — the shared renderables both rich hosts reuse.

These focus on :func:`render_diff`, the claude-code-style unified-diff colouriser:
a line-number gutter, filled +/- colour bars, cyan ``@@`` hunk headers, and
word-level highlight of only the spans that actually changed within a matched
-/+ pair. The builders are console-free (they return a ``rich.Text``), so the
tests inspect ``.plain`` (layout: gutter + content) and ``.spans`` (the palette
style tokens) directly — no ``Console`` needed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mote.cli.consumers.render.builders import (
    _HAS_RICH,
    compaction_summary_text,
    format_usage_line,
    interpolate_color,
    is_collapsible_tool,
    linkify,
    render_diff,
    render_file_change,
    render_image,
    shimmer_text,
    sparkline,
    tool_group_summary_text,
    tool_started_text,
)
from mote.cli.consumers.render.palette import Palette

pytestmark = pytest.mark.skipif(not _HAS_RICH, reason="rich required")


def _styles(text) -> list[str]:
    return [s.style for s in text.spans]


def test_diff_renders_line_number_gutter():
    # The gutter numbers begin only after a ``@@`` header anchors the counters.
    t = render_diff("@@ -1,3 +1,3 @@\n unchanged\n-const y = 2\n+const x = 1")
    lines = t.plain.splitlines()
    # hunk header row (no numbers), then a context row numbered old+new,
    # then the del (old-side number only) and add (new-side number only).
    assert lines[1].startswith("1 1 ")  # context: old=1 new=1
    assert "unchanged" in lines[1]
    assert lines[2].startswith("2   ")  # del: old=2, new blank
    assert "const y = 2" in lines[2]
    assert lines[3].startswith("  2 ")  # add: old blank, new=2
    assert "const x = 1" in lines[3]


def test_diff_add_and_del_get_filled_colour_bars():
    t = render_diff("@@ -1 +1 @@\n-old\n+new")
    styles = _styles(t)
    # Both the base background bars and the bright foregrounds are present.
    assert any(Palette.DIFF_ADD_BG in s for s in styles)
    assert any(Palette.DIFF_DEL_BG in s for s in styles)
    assert any(Palette.DIFF_ADD in s for s in styles)
    assert any(Palette.DIFF_DEL in s for s in styles)


def test_diff_word_level_emphasis_lights_only_the_delta():
    # A matched -/+ pair sharing "const" + "= 1" → only the changed word (y→x)
    # gets the brighter emphasis background; the equal runs stay on the base bar.
    t = render_diff("@@ -1 +1 @@\n-const y = 1\n+const x = 1")
    styles = _styles(t)
    assert any(Palette.DIFF_DEL_EMPH_BG in s for s in styles)
    assert any(Palette.DIFF_ADD_EMPH_BG in s for s in styles)


def test_diff_hunk_header_is_highlighted():
    t = render_diff("@@ -10,2 +10,2 @@\n context")
    assert any(Palette.DIFF_HUNK in s for s in _styles(t))
    assert "@@ -10,2 +10,2 @@" in t.plain


def test_diff_without_hunk_header_leaves_gutter_blank():
    # A headerless preamble never invents bogus line numbers.
    t = render_diff("--- a.py\n+++ b.py\n+added")
    lines = t.plain.splitlines()
    assert lines[0] == "--- a.py"  # meta line, no gutter
    assert lines[1] == "+++ b.py"
    assert "added" in lines[2]


def test_diff_empty_input_is_empty_text():
    assert render_diff("").plain == ""


# --- render_file_change: the structured old/new entry point ----------------


def test_file_change_synthesizes_diff_from_old_new():
    # An update: the changed line lights up on both sides, the equal line stays.
    t = render_file_change("a = 1\nb = 2\n", "a = 1\nb = 20\n", path="mod.py")
    plain = t.plain
    assert "b = 2" in plain  # the old body appears on a - bar
    assert "b = 20" in plain  # the new body appears on a + bar
    styles = _styles(t)
    assert any(Palette.DIFF_ADD_BG in s for s in styles)
    assert any(Palette.DIFF_DEL_BG in s for s in styles)


def test_file_change_creation_is_all_additions():
    # A creation (old == "") synthesizes a pure-addition diff.
    t = render_file_change("", "line one\nline two\n", path="new.py")
    styles = _styles(t)
    assert any(Palette.DIFF_ADD_BG in s for s in styles)
    assert not any(Palette.DIFF_DEL_BG in s for s in styles)
    assert "line one" in t.plain
    assert "line two" in t.plain


def test_file_change_deletion_is_all_removals():
    # A deletion (new == "") synthesizes a pure-removal diff.
    t = render_file_change("gone one\ngone two\n", "", path="old.py")
    styles = _styles(t)
    assert any(Palette.DIFF_DEL_BG in s for s in styles)
    assert not any(Palette.DIFF_ADD_BG in s for s in styles)
    assert "gone one" in t.plain


def test_file_change_word_level_emphasis_lights_only_the_delta():
    # Structured old/new share word-level highlighting with the text entry.
    t = render_file_change("const y = 1\n", "const x = 1\n", path="f.py")
    styles = _styles(t)
    assert any(Palette.DIFF_DEL_EMPH_BG in s for s in styles)
    assert any(Palette.DIFF_ADD_EMPH_BG in s for s in styles)


def test_file_change_identical_old_new_is_empty():
    # No delta → difflib emits no hunks → empty text.
    assert render_file_change("same\n", "same\n").plain == ""


def test_render_image_paints_half_block_cells(tmp_path):
    """A real image renders to a truecolor half-block renderable (rich Segments).

    Downscaled to fit the cell budget, each character cell stacks two pixels via
    ``▀`` — so rendering to a truecolor console yields the half-block glyph and
    24-bit SGR, no terminal image protocol needed.
    """
    pytest.importorskip("PIL")
    from io import StringIO

    from PIL import Image
    from rich.console import Console

    img = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(str(img))

    renderable = render_image(str(img))
    assert renderable is not None

    console = Console(file=StringIO(), color_system="truecolor", width=80)
    console.print(renderable)
    out = console.file.getvalue()
    assert "\u2580" in out  # ▀ half-block glyph
    assert "38;2;" in out  # 24-bit truecolor foreground SGR


def test_render_image_missing_file_returns_none():
    """A path that can't be opened degrades to ``None`` (caller shows text)."""
    assert render_image("/no/such/image.png") is None


# --- linkify: bare URLs → clickable link spans -----------------------------


def _link_span(text):
    """Return the first span whose style carries a ``link`` (or ``None``)."""
    from rich.style import Style

    for s in text.spans:
        style = s.style
        if isinstance(style, str):
            style = Style.parse(style)
        if getattr(style, "link", None):
            return s
    return None


def test_linkify_carries_a_link_style_on_the_url_span():
    t = linkify("see https://example.com now")
    span = _link_span(t)
    assert span is not None
    assert span.style.link == "https://example.com"


def test_linkify_keeps_base_style_on_surrounding_prose():
    t = linkify("go to https://example.com ok", base_style=Palette.DIM)
    # The non-URL prose keeps the caller's base style; only the URL span links.
    assert t.plain == "go to https://example.com ok"
    assert any(Palette.DIM in str(s.style) for s in t.spans)


def test_linkify_trims_trailing_sentence_punctuation():
    # A trailing period is prose, not part of the URL.
    t = linkify("visit https://example.com.")
    span = _link_span(t)
    assert span is not None
    assert span.style.link == "https://example.com"
    assert t.plain.endswith(".")


def test_linkify_keeps_balanced_paren_inside_url():
    # A closing paren that balances one inside the URL stays part of the link.
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    t = linkify(f"see {url}")
    span = _link_span(t)
    assert span is not None
    assert span.style.link == url


def test_linkify_plain_text_without_url_is_unchanged():
    t = linkify("no links here", base_style=Palette.DIM)
    assert t.plain == "no links here"
    assert _link_span(t) is None


def test_linkify_emits_osc8_when_rendered():
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), force_terminal=True, color_system="truecolor", width=80)
    console.print(linkify("see https://example.com now"))
    out = console.file.getvalue()
    assert "\x1b]8;;" in out  # OSC 8 hyperlink introducer
    assert "https://example.com" in out


# --------------------------------------------------------------------------
# compaction_summary_text — the folded recap re-rendered after a screen clear
# --------------------------------------------------------------------------


def test_compaction_summary_shows_short_recap_verbatim():
    t = compaction_summary_text("line one\nline two")
    assert t.plain == "line one\nline two"


def test_compaction_summary_folds_long_recap_with_count():
    body = "\n".join(f"l{i}" for i in range(20))
    t = compaction_summary_text(body, max_lines=12)
    # 12 kept lines + one "… +8 行" fold tail.
    assert "l0" in t.plain and "l11" in t.plain
    assert "l12" not in t.plain
    assert "… +8 行" in t.plain


def test_compaction_summary_empty_is_empty_text():
    assert compaction_summary_text("   ").plain == ""


# --------------------------------------------------------------------------
# consecutive search/read tool grouping (claude-code collapseReadSearch)
# --------------------------------------------------------------------------


def test_is_collapsible_tool_covers_search_and_read():
    assert is_collapsible_tool("Read")
    assert is_collapsible_tool("Grep")
    assert is_collapsible_tool("Glob")
    assert not is_collapsible_tool("Write")
    assert not is_collapsible_tool("Edit")
    assert not is_collapsible_tool("Bash")


def test_tool_group_summary_counts_search_and_read():
    items = [("Grep", "foo"), ("Glob", "*.py"), ("Read", "/a.py")]
    t = tool_group_summary_text(items, active=False, expanded=False)
    assert "搜索 2 个模式" in t.plain
    assert "读取 1 个文件" in t.plain


def test_tool_group_summary_dedupes_read_paths():
    # The same file read twice counts once (claude-code Set dedupe).
    items = [("Read", "/a.py"), ("Read", "/a.py"), ("Read", "/b.py")]
    t = tool_group_summary_text(items, active=False, expanded=False)
    assert "读取 2 个文件" in t.plain
    assert "搜索" not in t.plain  # no search calls → no search phrase


def test_tool_group_summary_active_appends_ellipsis():
    items = [("Read", "/a.py")]
    active = tool_group_summary_text(items, active=True, expanded=False)
    idle = tool_group_summary_text(items, active=False, expanded=False)
    assert "…" in active.plain
    assert "…" not in idle.plain


def test_tool_group_summary_toggle_hint_reflects_state():
    items = [("Read", "/a.py")]
    assert "ctrl+o 展开" in tool_group_summary_text(items, active=False, expanded=False).plain
    assert "ctrl+o 折叠" in tool_group_summary_text(items, active=False, expanded=True).plain


def test_tool_group_summary_empty_items_is_empty_text():
    assert tool_group_summary_text([], active=False, expanded=False).plain == ""


# --------------------------------------------------------------------------
# format_usage_line — claude-code ` │ ` field separator
# --------------------------------------------------------------------------


def test_usage_line_joins_fields_with_pipe_separator():
    from types import SimpleNamespace

    ev = SimpleNamespace(
        model="gpt-4",
        total_tokens=1234,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.05,
        context_pct=0.42,
        context_used=None,
        context_window=None,
    )
    line = format_usage_line(ev)
    assert " \u2502 " in line  # dim vertical-rule separator, not "  "
    assert line == "gpt-4 \u2502 1,234 tok \u2502 $0.0500 \u2502 ctx 42%"


# --------------------------------------------------------------------------
# shimmer 微光 — the moving bright band across the spinner label
# --------------------------------------------------------------------------


def test_interpolate_color_blends_channels_at_fraction():
    assert interpolate_color("#000000", "#ffffff", 0.0) == "#000000"
    assert interpolate_color("#000000", "#ffffff", 1.0) == "#ffffff"
    assert interpolate_color("#000000", "#ffffff", 0.5) == "#808080"


def test_interpolate_color_clamps_out_of_range_t():
    assert interpolate_color("#102030", "#405060", -1.0) == "#102030"
    assert interpolate_color("#102030", "#405060", 2.0) == "#405060"


def test_shimmer_preserves_plain_text():
    assert shimmer_text("思考中", frame=0).plain == "思考中"
    assert shimmer_text("", frame=3).plain == ""


def test_shimmer_lights_the_char_under_the_band_centre():
    # At frame == index the band centre sits on that char (dist 0 → t 1.0), so it
    # gets the full bright shimmer colour; a char well outside stays on base.
    t = shimmer_text("hello", frame=2, radius=1, pad=6)
    styles = _styles(t)
    assert f"bold {Palette.SHIMMER}" == styles[2]  # centre char fully bright
    assert f"bold {Palette.BRAND}" == styles[4]  # far char stays on base


def test_shimmer_band_moves_with_the_frame():
    a = _styles(shimmer_text("hello", frame=1, radius=1, pad=6))
    b = _styles(shimmer_text("hello", frame=3, radius=1, pad=6))
    assert a != b  # advancing the frame slides the bright band


# --------------------------------------------------------------------------
# sparkline 图表 — sub-cell block mini bar-chart
# --------------------------------------------------------------------------


def test_sparkline_maps_ascending_values_low_to_high():
    t = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(t.plain) == 8
    assert t.plain[0] == "\u2581"  # ▁ lowest
    assert t.plain[-1] == "\u2588"  # █ highest


def test_sparkline_flat_series_is_a_mid_bar():
    # All-equal values have no span → a mid-height bar, not empty or full.
    t = sparkline([5, 5, 5])
    assert set(t.plain) == {"\u2585"}  # ▅ (mid glyph), all three identical


def test_sparkline_empty_or_nonnumeric_is_empty_text():
    assert sparkline([]).plain == ""
    assert sparkline(["a", None]).plain == ""


# --------------------------------------------------------------------------
# tool bullet status colour (#3) — brand running, green/red on completion
# --------------------------------------------------------------------------


def _bullet_style(text) -> str:
    return text.spans[0].style


def test_tool_bullet_is_brand_while_running():
    ev = SimpleNamespace(title=None, tool_name="Bash", headline="ls")
    assert _bullet_style(tool_started_text(ev)) == Palette.BRAND


def test_tool_bullet_pulses_shimmer_when_blinking():
    ev = SimpleNamespace(title=None, tool_name="Bash", headline="ls")
    assert _bullet_style(tool_started_text(ev, blink=True)) == Palette.SHIMMER


def test_tool_bullet_turns_green_on_success_red_on_failure():
    ev = SimpleNamespace(title=None, tool_name="Bash", headline="ls")
    assert _bullet_style(tool_started_text(ev, ok=True)) == Palette.SUCCESS
    assert _bullet_style(tool_started_text(ev, ok=False)) == Palette.ERROR
