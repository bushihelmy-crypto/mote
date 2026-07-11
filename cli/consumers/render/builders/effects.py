#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shimmer 微光 + sparkline 图表: animated / data mini-renderables.

claude-code's spinner sweeps a bright band across the "working" label (a shimmer
/ 微光), computed frame-by-frame; and it draws sub-cell block bars. Both are pure
functions of their inputs (the frame counter / the value list), so they live here
as console-free builders the Textual host drives from its 0.1s heartbeat.
"""

from __future__ import annotations

from typing import Any, Tuple

from mote.cli.consumers.render.builders._rich import Text
from mote.cli.consumers.render.palette import Palette


def _hex_to_rgb(color: str) -> Tuple[int, int, int]:
    """Parse ``#rrggbb`` → ``(r, g, b)`` (no validation — palette tokens are hex)."""
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def interpolate_color(a: str, b: str, t: float) -> str:
    """Linearly blend two ``#rrggbb`` colours → ``#rrggbb`` at fraction ``t`` (0→a, 1→b).

    Mirrors claude-code's ``interpolateColor`` (per-channel lerp). ``t`` is clamped
    to ``[0, 1]`` so an out-of-range frame can't produce a bogus byte.
    """
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def shimmer_text(
    text: str,
    frame: int,
    *,
    base: str = Palette.BRAND,
    bright: str = Palette.SHIMMER,
    radius: int = 2,
    pad: int = 6,
    bold: bool = True,
) -> "Text":
    """A shimmering label — a bright band sweeping left-to-right across *text*.

    Mirrors claude-code's ``useShimmerAnimation``/``GlimmerMessage``: the band
    centre advances one cell per *frame*, brightest at the centre and fading to
    *base* within ``±radius`` cells (colours blended via :func:`interpolate_color`).
    The cycle length is ``len(text) + pad`` so the band runs off the right edge and
    rests briefly before wrapping (the *pad* is the off-screen pause). The plain
    text is unchanged — only the per-character colour moves — so callers can still
    assert on ``.plain``. Empty *text* → empty ``Text``.
    """
    out = Text()
    if not text:
        return out
    n = len(text)
    centre = frame % (n + max(1, pad))
    for i, ch in enumerate(text):
        dist = abs(i - centre)
        if dist > radius:
            colour = base
        else:
            # 1.0 at the centre → 0.0 just past the radius edge.
            colour = interpolate_color(base, bright, 1.0 - dist / (radius + 1))
        out.append(ch, style=f"bold {colour}" if bold else colour)
    return out


# Eight sub-cell heights (U+2581..U+2588) — the sparkline / mini-bar alphabet.
_SPARK_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sparkline(values: Any, *, style: str = Palette.DIM) -> "Text":
    """A one-line ``▁▂▃▅█`` mini bar-chart of *values* (claude-code block glyphs).

    Each value maps to one of eight sub-cell block heights, scaled between the
    series min and max so the shape reads as a trend regardless of magnitude. A
    flat series (all equal, e.g. a single sample repeated) renders a mid-height bar
    rather than an empty/full one. Empty/non-numeric *values* → empty ``Text``.
    """
    out = Text()
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return out
    lo, hi = min(nums), max(nums)
    span = hi - lo
    top = len(_SPARK_BLOCKS) - 1
    for v in nums:
        idx = len(_SPARK_BLOCKS) // 2 if span == 0 else round((v - lo) / span * top)
        out.append(_SPARK_BLOCKS[idx], style=style)
    return out


__all__ = ["interpolate_color", "shimmer_text", "sparkline"]
