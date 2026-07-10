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

from metagpt.cli.consumers.render.builders._rich import _HAS_RICH

_HALF_BLOCK = "\u2580"  # ▀ UPPER HALF BLOCK
# These are sanity CAPS, not the target size: at render time the image is sized to
# the host's live content width (``options.max_width``), so on a wide terminal it
# fills the window (== as sharp as this protocol-free method gets). The caps only
# stop a huge window from rasterizing an absurdly large grid.
IMAGE_MAX_COLS = 240  # character columns == image pixels wide (ceiling)
IMAGE_MAX_ROWS = 120  # character rows; each stacks two image pixels tall (ceiling)


def _resample_filter():
    """The sharpest available Pillow downscale filter (LANCZOS → BICUBIC)."""
    from PIL import Image

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    if resample is None:  # pragma: no cover — pre-9.1 Pillow only
        resample = getattr(Image, "BICUBIC", 3)
    return resample


class _HalfBlockImage:
    """Rich renderable painting an image as ``▀`` half-block cells.

    Holds the *full-resolution* RGB image and only rasterizes at render time,
    when the host tells us the real available width (``options.max_width``). A
    character cell packs one horizontal pixel and — via the upper/lower half of
    ``▀`` (foreground/background) — two vertical pixels, so the clarity ceiling of
    this (protocol-free) method is exactly the terminal's column count. Sizing to
    the live width therefore fills that ceiling instead of a guessed constant: a
    wide window gets a big, crisp image; a narrow one shrinks to fit without
    clipping. ``max_rows`` still caps height so a tall image can't scroll forever.
    """

    def __init__(self, image: Any, *, max_cols: int, max_rows: int) -> None:
        self._image = image  # a full-res PIL RGB Image
        self._max_cols = max_cols
        self._max_rows = max_rows

    def _raster(self, cols: int) -> list:
        """Downscale the held image to ``cols`` wide (aspect-kept) → RGB pixel rows."""
        width, height = self._image.size
        # Fit within the live column budget AND the row cap; never upscale past 1.0
        # (enlarging a small image only invents blur).
        scale = min(cols / width, (self._max_rows * 2) / height, 1.0)
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        if new_h % 2:  # even height so rows pair cleanly into cells
            new_h += 1
        im = self._image.resize((new_w, new_h), resample=_resample_filter())
        px = im.load()
        return [[px[x, y] for x in range(new_w)] for y in range(new_h)]

    def __rich_console__(self, console: Any, options: Any):  # noqa: ANN001
        from rich.color import Color
        from rich.segment import Segment
        from rich.style import Style

        # Use the live content width the host offers, clamped to our sanity cap,
        # so the image is as large (== as sharp) as the current window allows.
        cols = min(self._max_cols, max(1, options.max_width))
        rows = self._raster(cols)
        for y in range(0, len(rows), 2):
            top = rows[y]
            bottom = rows[y + 1] if y + 1 < len(rows) else None
            for x in range(len(top)):
                tr, tg, tb = top[x]
                if bottom is not None:
                    br, bg, bb = bottom[x]
                    style = Style(color=Color.from_rgb(tr, tg, tb), bgcolor=Color.from_rgb(br, bg, bb))
                else:
                    style = Style(color=Color.from_rgb(tr, tg, tb))
                yield Segment(_HALF_BLOCK, style)
            yield Segment.line()


def render_image(source: Any, *, max_cols: int = IMAGE_MAX_COLS, max_rows: int = IMAGE_MAX_ROWS) -> Optional[Any]:
    """Render an image file to a truecolor half-block renderable, or ``None``.

    ``source`` is a filesystem path. The returned renderable defers scaling to
    render time, sizing the image to the host's *live* content width (up to
    ``max_cols``) with high-quality LANCZOS resampling — so it fills the terminal
    for maximum sharpness rather than a fixed small grid, while ``2·max_rows`` caps
    the height. Returns ``None`` — so the caller can fall back to a text reference
    — when ``rich`` or Pillow is unavailable or the image cannot be opened/decoded.
    """
    if not _HAS_RICH:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(source) as im:
            im = im.convert("RGB")
            if im.width == 0 or im.height == 0:
                return None
            im = im.copy()  # detach from the file handle closed by the ``with``
    except Exception:  # noqa: BLE001 — any decode failure degrades to text
        return None
    return _HalfBlockImage(im, max_cols=max_cols, max_rows=max_rows)


__all__ = ["render_image", "IMAGE_MAX_COLS", "IMAGE_MAX_ROWS"]
