"""Independent-window presenter for a persistent PTY Terminal Runtime."""
from __future__ import annotations

from mote.contracts.ports.surface.window import LiveWindowBackend
from mote.contracts.surface import TERMINAL_MEDIA_TYPE
from mote.product.interfaces.textual.surfaces.live_window import LiveWindowPresenter


class TerminalWindowPresenter(LiveWindowPresenter):
    """Present a PTY surface in the shared standalone viewer lifecycle."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(
            backend,
            surface_kind="terminal",
            media_type=TERMINAL_MEDIA_TYPE,
        )


__all__ = ["TerminalWindowPresenter"]
