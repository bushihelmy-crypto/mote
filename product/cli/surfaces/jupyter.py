"""Independent-window presenter for a persistent Jupyter Runtime."""
from __future__ import annotations

from mote.contracts.notebook import NOTEBOOK_MEDIA_TYPE
from mote.contracts.ports.window_surface import LiveWindowBackend
from mote.product.cli.surfaces.live_window import LiveWindowPresenter


class JupyterWindowPresenter(LiveWindowPresenter):
    """Present a typed notebook surface in the shared standalone viewer."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(
            backend,
            surface_kind="notebook",
            media_type=NOTEBOOK_MEDIA_TYPE,
        )


__all__ = ["JupyterWindowPresenter"]
