#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pluggable native terminal image protocols (pixel-perfect inline images).

The half-block renderer (:func:`builders.render_image`) works in *any* truecolor
terminal but is capped by the character grid (one horizontal pixel per cell, two
vertical via ``▀``). A terminal that speaks a real *image protocol* can instead
paint the raw bitmap — true pixel resolution, no grid quantization.

This module is the extension point for those protocols, designed so adding a new
one (iTerm2, Sixel, …) is a *closed* change: implement
:class:`TerminalImageProtocol`, register it, done — no call site changes. Each
protocol owns three concerns behind a stable contract:

* ``detect()`` — is *this* terminal (right now) able to render me? Cheap and
  side-effect-free by default (environment sniffing); a protocol that needs a
  real capability handshake overrides the reserved :meth:`probe` hook instead of
  polluting ``detect``.
* ``encode(path, ...)`` — turn an image file into the terminal's escape sequence.
* ``name`` — a stable id for logging / tests / preference ordering.

:func:`detect_image_protocol` walks the registry in priority order and returns
the first protocol that reports itself available (or ``None`` — the caller then
falls back to half-blocks). The registry is the *only* thing a new protocol
touches, which is what keeps this future-proof: the detection contract is fixed,
the set of implementations grows.
"""

from __future__ import annotations

import base64
import os
from typing import List, Optional, Type

try:
    from mote.product.presentation.rich_rendering.pillow_encoding import png_bytes as _pillow_png_bytes
except ImportError:  # pragma: no cover — Pillow is absent
    _pillow_png_bytes = None

# Kitty streams pixel data in fixed-size base64 chunks (the protocol caps each
# APC payload at 4096 bytes of *base64*); 4096 is the canonical chunk size.
_KITTY_CHUNK = 4096


class TerminalImageProtocol:
    """A terminal's native inline-image protocol — the closed extension point.

    Subclasses implement :meth:`detect` (availability) and :meth:`encode`
    (image → escape sequence). :meth:`probe` is a reserved, opt-in hook for a
    *dynamic* capability handshake (send a query, read the reply) — deliberately
    unused today so the whole layer stays side-effect-free, but present so a
    future protocol can upgrade detection accuracy without changing the contract
    every caller depends on.
    """

    #: Stable identifier (``"kitty"`` / ``"iterm2"`` / ``"sixel"``). Used for
    #: preference ordering, logging, and tests.
    name: str = "base"

    def detect(self) -> bool:
        """True iff this terminal can render this protocol (cheap, no side effects).

        Default is a pure environment sniff. A protocol MUST NOT touch stdin or
        emit query sequences here — that belongs in :meth:`probe`.
        """
        return False

    def probe(self) -> Optional[bool]:
        """Reserved dynamic capability handshake (send query, read reply).

        Returns ``True``/``False`` when a live handshake conclusively answers, or
        ``None`` (the default) when this protocol does no handshake and defers to
        :meth:`detect`. Kept unimplemented on purpose: it needs raw-mode stdin
        with a timeout and risks output contamination, so we only reach for it if
        environment detection ever proves insufficient.
        """
        return None

    def encode(self, path: str, *, max_cols: int = 0, max_rows: int = 0) -> Optional[str]:
        """Encode the image at *path* into this terminal's escape sequence.

        ``max_cols`` / ``max_rows`` are optional cell budgets (0 = unconstrained)
        the terminal uses to scale the image into the text grid. Returns ``None``
        when the image can't be read/encoded so the caller can fall back.
        """
        raise NotImplementedError


class KittyImageProtocol(TerminalImageProtocol):
    """The Kitty graphics protocol (kitty, Ghostty, recent WezTerm).

    Transmits a PNG payload as base64 inside APC ``_G`` escapes: a control-data
    header (``f=100`` PNG, ``a=T`` transmit-and-display) followed by the data in
    ``m=1`` continuation chunks (``m=0`` on the last). The terminal decodes and
    paints the real bitmap, so resolution is the image's own — bounded only by
    the cell box (``c``/``r``) we ask it to fit into.
    """

    name = "kitty"

    def detect(self) -> bool:
        """Sniff the environment for a Kitty-graphics-capable terminal.

        kitty sets ``KITTY_WINDOW_ID`` and a ``TERM`` of ``xterm-kitty``;
        Ghostty advertises ``TERM_PROGRAM=ghostty``; recent WezTerm speaks Kitty
        graphics and identifies via ``TERM_PROGRAM=WezTerm`` / ``WEZTERM_*``.
        """
        env = os.environ
        if env.get("KITTY_WINDOW_ID"):
            return True
        if "kitty" in (env.get("TERM", "") or "").lower():
            return True
        term_program = (env.get("TERM_PROGRAM", "") or "").lower()
        if term_program in ("ghostty", "wezterm"):
            return True
        if env.get("GHOSTTY_RESOURCES_DIR") or env.get("WEZTERM_EXECUTABLE"):
            return True
        return False

    def encode(self, path: str, *, max_cols: int = 0, max_rows: int = 0) -> Optional[str]:
        """Build the APC ``_G`` escape stream that transmits + displays the PNG.

        Re-encodes whatever the source is to PNG (``f=100``) via Pillow so the
        payload is always a format the protocol mandates; degrades to raw bytes
        with ``f=100`` only when they already look like a PNG. Returns ``None`` on
        any read/encode failure.
        """
        data = self._png_bytes(path)
        if not data:
            return None

        # Control keys shared by the whole image: PNG format, transmit+display.
        # ``c``/``r`` (columns/rows) fit the bitmap into a cell box when given, so
        # a huge image doesn't blow past the viewport; 0 means "let kitty decide".
        base_ctrl = "f=100,a=T"
        if max_cols > 0:
            base_ctrl += f",c={max_cols}"
        if max_rows > 0:
            base_ctrl += f",r={max_rows}"

        b64 = base64.standard_b64encode(data).decode("ascii")
        chunks = [b64[i : i + _KITTY_CHUNK] for i in range(0, len(b64), _KITTY_CHUNK)] or [""]

        out: List[str] = []
        for idx, chunk in enumerate(chunks):
            first = idx == 0
            last = idx == len(chunks) - 1
            more = 0 if last else 1
            # Only the first chunk carries the image control keys; every chunk
            # carries ``m`` (1 = more data follows, 0 = final). APC = ESC _ … ESC \.
            ctrl = f"{base_ctrl},m={more}" if first else f"m={more}"
            out.append(f"\x1b_G{ctrl};{chunk}\x1b\\")
        return "".join(out)

    @staticmethod
    def _png_bytes(path: str) -> Optional[bytes]:
        """Return PNG bytes for *path* (re-encoding via Pillow), or ``None``."""
        if _pillow_png_bytes is not None:
            return _pillow_png_bytes(path)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        return raw if raw[:8] == b"\x89PNG\r\n\x1a\n" else None


# The protocol registry, in preference order. A new protocol is added here (and
# as a class above) with zero changes to any caller — the closed extension point.
_REGISTRY: List[Type[TerminalImageProtocol]] = [
    KittyImageProtocol,
]

# Detection is process-stable (the terminal doesn't change mid-run), so we cache
# the first result. ``_UNSET`` distinguishes "not yet detected" from "detected
# nothing" (both are falsy otherwise).
_UNSET = object()
_cached_protocol: object = _UNSET


def detect_image_protocol(*, force: bool = False) -> Optional[TerminalImageProtocol]:
    """Return the first available native image protocol, or ``None``.

    Walks :data:`_REGISTRY` in priority order and returns an instance of the
    first protocol whose :meth:`~TerminalImageProtocol.detect` is true. The
    result is cached for the process (pass ``force=True`` to re-detect, e.g. in
    tests that mutate ``os.environ``). ``None`` means no native protocol — the
    caller falls back to half-block rendering.
    """
    global _cached_protocol
    if not force and _cached_protocol is not _UNSET:
        return _cached_protocol  # type: ignore[return-value]
    found: Optional[TerminalImageProtocol] = None
    for proto_cls in _REGISTRY:
        proto = proto_cls()
        if proto.detect():
            found = proto
            break
    _cached_protocol = found
    return found


__all__ = [
    "TerminalImageProtocol",
    "KittyImageProtocol",
    "detect_image_protocol",
]
