"""Pillow + Rich implementation of the half-block image renderer."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from PIL import Image
from rich.color import Color
from rich.segment import Segment
from rich.style import Style

_HALF_BLOCK = "\u2580"


def _resample_filter():
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    if resample is None:  # pragma: no cover — pre-9.1 Pillow only
        resample = getattr(Image, "BICUBIC", 3)
    return resample


class HalfBlockImage:
    """Rich renderable that packs two image pixels into each terminal cell."""

    _CACHE_SIZE = 4

    def __init__(self, image: Any, *, max_cols: int, max_rows: int) -> None:
        self._image = image
        self._max_cols = max_cols
        self._max_rows = max_rows
        self._segments_by_cols: OrderedDict[int, tuple[Segment, ...]] = OrderedDict()

    def _raster(self, cols: int) -> list:
        width, height = self._image.size
        scale = min(cols / width, (self._max_rows * 2) / height, 1.0)
        new_width = max(1, round(width * scale))
        new_height = max(1, round(height * scale))
        if new_height % 2:
            new_height += 1
        image = self._image.resize((new_width, new_height), resample=_resample_filter())
        pixels = image.load()
        return [[pixels[x, y] for x in range(new_width)] for y in range(new_height)]

    def _render_segments(self, cols: int) -> tuple[Segment, ...]:
        rows = self._raster(cols)
        segments: list[Segment] = []
        for y in range(0, len(rows), 2):
            top = rows[y]
            bottom = rows[y + 1] if y + 1 < len(rows) else None
            for x, (red, green, blue) in enumerate(top):
                if bottom is None:
                    style = Style(color=Color.from_rgb(red, green, blue))
                else:
                    bg_red, bg_green, bg_blue = bottom[x]
                    style = Style(
                        color=Color.from_rgb(red, green, blue),
                        bgcolor=Color.from_rgb(bg_red, bg_green, bg_blue),
                    )
                segments.append(Segment(_HALF_BLOCK, style))
            segments.append(Segment.line())
        return tuple(segments)

    def __rich_console__(self, console: Any, options: Any):  # noqa: ANN001
        cols = min(self._max_cols, max(1, options.max_width))
        segments = self._segments_by_cols.get(cols)
        if segments is None:
            segments = self._render_segments(cols)
            self._segments_by_cols[cols] = segments
            if len(self._segments_by_cols) > self._CACHE_SIZE:
                self._segments_by_cols.popitem(last=False)
        else:
            self._segments_by_cols.move_to_end(cols)
        yield from segments


def render_image(source: Any, *, max_cols: int, max_rows: int) -> HalfBlockImage | None:
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            if image.width == 0 or image.height == 0:
                return None
            image = image.copy()
    except Exception:  # noqa: BLE001 — decode failure degrades to text
        return None
    return HalfBlockImage(image, max_cols=max_cols, max_rows=max_rows)


__all__ = ["HalfBlockImage", "render_image"]
