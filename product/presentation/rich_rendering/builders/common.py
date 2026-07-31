"""Layout primitives shared by rich-rendering domains."""

from __future__ import annotations

from typing import Any

from mote.product.presentation.rich_rendering.builders._rich import Padding, Table, Text
from mote.product.presentation.rich_rendering.palette import Palette

CONTENT_INDENT = 2
RESULT_INDENT = 4


def notice_style(level: str) -> str:
    return {"warning": Palette.WARNING, "success": Palette.SUCCESS}.get(level, Palette.DIM)


def bullet_row(glyph: str, renderable: Any, *, style: str) -> Any:
    grid = Table.grid(padding=(0, 0))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(Text(glyph + " ", style=style), renderable)
    return grid


def indent(renderable: Any, spaces: int = CONTENT_INDENT) -> Any:
    return Padding(renderable, (0, 0, 0, spaces))


__all__ = [
    "CONTENT_INDENT",
    "RESULT_INDENT",
    "bullet_row",
    "indent",
    "notice_style",
]
