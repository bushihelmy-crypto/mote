"""Session-list renderables."""

from __future__ import annotations

from typing import Optional

from mote.product.presentation.events import SessionListShown
from mote.product.presentation.rich_rendering.builders._rich import Table, box
from mote.product.presentation.rich_rendering.palette import Palette


def session_table(ev: SessionListShown) -> Optional["Table"]:
    """Build the numbered resumable-session table."""
    if not ev.items:
        return None
    table = Table(
        title=ev.title,
        show_header=True,
        header_style=f"bold {Palette.BRAND}",
        box=box.SIMPLE,
    )
    table.add_column("#", style=Palette.BRAND, justify="right")
    table.add_column("Session")
    table.add_column("Updated", style=Palette.DIM)
    table.add_column("Preview", style=Palette.DIM)
    for item in ev.items:
        table.add_row(
            str(item.index),
            item.label or item.session_id,
            item.updated_at or "",
            item.preview or "",
        )
    return table


__all__ = ["session_table"]
