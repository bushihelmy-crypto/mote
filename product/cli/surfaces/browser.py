"""Legacy screenshot presenter for browser observation surfaces."""
from __future__ import annotations

from mote.contracts.ports.window_surface import LiveWindowBackend
from mote.product.cli.surfaces.live_window import LiveWindowPresenter

_BROWSER_MEDIA_TYPE = "application/vnd.mote.browser+json"


class BrowserWindowPresenter(LiveWindowPresenter):
    """Present browser observation frames when a host explicitly requests them."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(
            backend,
            surface_kind="browser",
            media_type=_BROWSER_MEDIA_TYPE,
        )


__all__ = ["BrowserWindowPresenter"]
