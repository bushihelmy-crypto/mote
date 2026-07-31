#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Protocol-free inline image rendering via ``▀`` half-block cells.

Two vertical pixels per character cell (``▀`` with the top pixel as foreground,
the bottom as background). Pure 24-bit SGR, so it needs no terminal image
protocol (sixel/kitty/iTerm) — it paints in any truecolor terminal AND in a
Textual widget. Callers degrade to a text reference when :func:`render_image`
returns ``None`` (rich or Pillow absent, or the image can't be decoded).
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from mote.product.presentation.rich_rendering.builders.image_adapter import render_image as _render_image
except ImportError:  # pragma: no cover — Rich or Pillow is absent
    _render_image = None

# These are sanity CAPS, not the target size: at render time the image is sized to
# the host's live content width (``options.max_width``), so on a wide terminal it
# fills the window (== as sharp as this protocol-free method gets). The caps only
# stop a huge window from rasterizing an absurdly large grid.
IMAGE_MAX_COLS = 240  # character columns == image pixels wide (ceiling)
IMAGE_MAX_ROWS = 120  # character rows; each stacks two image pixels tall (ceiling)


def render_image(source: Any, *, max_cols: int = IMAGE_MAX_COLS, max_rows: int = IMAGE_MAX_ROWS) -> Optional[Any]:
    """Render an image file to a truecolor half-block renderable, or ``None``.

    ``source`` is a filesystem path. The returned renderable defers scaling to
    render time, sizing the image to the host's *live* content width (up to
    ``max_cols``) with high-quality LANCZOS resampling — so it fills the terminal
    for maximum sharpness rather than a fixed small grid, while ``2·max_rows`` caps
    the height. Returns ``None`` — so the caller can fall back to a text reference
    — when ``rich`` or Pillow is unavailable or the image cannot be opened/decoded.
    """
    if _render_image is None:
        return None
    return _render_image(source, max_cols=max_cols, max_rows=max_rows)


__all__ = ["render_image", "IMAGE_MAX_COLS", "IMAGE_MAX_ROWS"]
