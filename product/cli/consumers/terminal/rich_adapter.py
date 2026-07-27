"""Rich terminal consumer adapter, imported only when Rich is available."""
from __future__ import annotations

from mote.product.cli.consumers.terminal.surface import TerminalSurface
from mote.product.cli.consumers.transcript import SurfaceDriver


def build_rich_terminal_consumer() -> SurfaceDriver:
    return SurfaceDriver(TerminalSurface())


__all__ = ["build_rich_terminal_consumer"]
