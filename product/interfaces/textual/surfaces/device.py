"""Independent-window presenter for an external Device Runtime surface."""
from __future__ import annotations

from mote.contracts.ports.surface.window import LiveWindowBackend
from mote.product.interfaces.textual.surfaces.live_window import LiveWindowPresenter

_DEVICE_MEDIA_TYPE = "application/vnd.mote.device+json"


class DeviceWindowPresenter(LiveWindowPresenter):
    """Present a Device Runtime in a separately fenced viewer window."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(
            backend,
            surface_kind="device",
            media_type=_DEVICE_MEDIA_TYPE,
        )


__all__ = ["DeviceWindowPresenter"]
