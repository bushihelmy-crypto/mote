#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared base widget for the Textual transcript rows.

:class:`SelectableStatic` is the common ancestor of every transcript row: it
makes rich renderables (``Markdown``/``Group``/rich ``Table``) mouse-selectable,
copyable and highlightable — none of which Textual supports natively for a
non-``Text`` visual — and follows a linkified URL span on Ctrl+click.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style as RichStyle
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Static


class SelectableStatic(Static):
    """A ``Static`` whose text stays mouse-selectable AND highlighted for ANY renderable.

    Textual only supports selection natively for widgets that render a plain
    ``Text``/``Content`` visual. Our transcript rows render rich renderables
    (``Markdown``, ``Group``, rich ``Table``) wrapped in a ``RichVisual``, which
    breaks the built-in behaviour on THREE counts, all fixed here:

    1. **Per-character drag-select** — the compositor maps a mouse coordinate to a
       character offset by scanning the rendered line's segments for an
       ``{"offset": (x, y)}`` style meta (``Compositor.get_widget_and_offset_at``).
       Only the ``Content`` visual embeds that meta; ``RichVisual`` segments carry
       none, so the compositor could never resolve an offset and every drag
       collapsed to a *whole-widget* selection (copying the entire block). Our
       ``render_line`` re-tags every segment with its ``(char_offset, y)`` so a
       drag now selects exactly the characters under the pointer.

    2. **Copy** — ``Widget.get_selection`` returns ``None`` for a non-Text visual,
       so the row can't be copied. We override it to reconstruct the text from the
       already-rendered strips (``_render_cache.lines``): each strip is exactly one
       *rendered* line, so its columns line up with the (y, x) offsets Textual's
       ``Selection`` slices by — correct even across wrapped lines. Trailing
       block-padding spaces are stripped so a copy doesn't carry the right fill.

    3. **Highlight** — ``RichVisual.render_strips`` ignores the selection style
       (only the ``Content`` visual applies it), so a drag over these rows copied
       fine but showed NO visible highlight band. ``render_line`` paints the
       ``screen--selection`` component style onto the selected span of each line
       (mirroring how Textual's own ``RichLog`` highlights its lines).
    """

    def get_selection(self, selection: Selection) -> Optional[tuple[str, str]]:
        cache = getattr(self, "_render_cache", None)
        if cache is None or not cache.lines:
            return None
        text = "\n".join(strip.text.rstrip() for strip in cache.lines)
        if not text.strip():
            return None
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        strip = self._tag_offsets(super().render_line(y), y)
        selection = self.text_selection
        if selection is None:
            return strip
        span = selection.get_span(y)
        if span is None:
            return strip
        return self._paint_selection(strip, span)

    def _tag_offsets(self, strip: Strip, y: int) -> Strip:
        """Embed ``{"offset": (char_x, y)}`` on every segment.

        This is the meta ``Compositor.get_widget_and_offset_at`` scans to turn a
        mouse coordinate into a character offset; without it a drag can't resolve
        a partial span and collapses to the whole widget. ``char_x`` is the running
        *character* count (the compositor converts cells→characters itself).
        """
        segments = []
        char_x = 0
        for seg in strip:
            base = seg.style or RichStyle()
            segments.append(
                Segment(seg.text, base + RichStyle.from_meta({"offset": (char_x, y)}), seg.control)
            )
            char_x += len(seg.text)
        return Strip(segments, strip.cell_length)

    def _paint_selection(self, strip: Strip, span: tuple[int, int]) -> Strip:
        """Paint the ``screen--selection`` band over the selected character span."""
        start, end = span
        text = strip.text
        if end == -1:
            end = len(text)
        if start >= end:
            return strip
        # ``span`` is in character offsets; ``Strip.divide`` cuts by cell columns —
        # convert so wide (CJK) glyphs highlight at the right width.
        start_cell = cell_len(text[:start])
        end_cell = cell_len(text[:end])
        if start_cell >= end_cell:
            return strip
        style = self.screen.get_component_rich_style("screen--selection")
        before, middle, after = strip.divide([start_cell, end_cell, strip.cell_length])
        # ``Strip.apply_style`` combines as ``style + segment.style`` so the row's
        # own background would win over the selection band; apply it as ``post_style``
        # (``segment.style + style``) so the highlight bg overrides instead.
        highlighted = Strip(
            list(Segment.apply_style(middle._segments, post_style=style)),
            middle.cell_length,
        )
        return Strip.join([before, highlighted, after])

    async def _on_click(self, event: Any) -> None:
        """Ctrl+click a linkified span → open its URL in the browser.

        Every URL span carries ``Style(link=url)`` (from the shared ``linkify`` /
        themed-markdown builders); the click event exposes it as ``event.style.link``
        for both markdown links and linkified plain text. We gate on ``event.ctrl``
        so a plain click still starts a text selection (matching claude-code, where
        the terminal only follows a link on Ctrl+click) and only intercept when the
        modifier is held AND the pointer is actually over a link span. Everything
        else falls through to the base handler so drag-select keeps working.
        """
        link = getattr(getattr(event, "style", None), "link", None)
        if link and getattr(event, "ctrl", False):
            event.stop()
            self.app.open_url(link)
            return
        await super()._on_click(event)


__all__ = ["SelectableStatic"]
