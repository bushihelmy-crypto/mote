"""Independent-window presenter for the canonical Browser surface."""

from __future__ import annotations

from mote.contracts.ports.surface.window import LiveWindowBackend
from mote.product.interfaces.textual.surfaces.live_window import LiveWindowPresenter

_BROWSER_MEDIA_TYPE = "application/vnd.mote.browser+json"


class BrowserWindowPresenter(LiveWindowPresenter):
    """Present a Browser Runtime in a separately fenced viewer window."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(backend, surface_kind="browser", media_type=_BROWSER_MEDIA_TYPE)


__all__ = ["BrowserWindowPresenter"]
