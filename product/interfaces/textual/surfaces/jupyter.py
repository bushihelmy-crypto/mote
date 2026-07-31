"""Independent-window presenter for a persistent Jupyter Runtime."""
from __future__ import annotations

from mote.contracts.ports.surface.window import LiveWindowBackend
from mote.contracts.surface import NOTEBOOK_MEDIA_TYPE
from mote.product.interfaces.textual.surfaces.live_window import LiveWindowPresenter


class JupyterWindowPresenter(LiveWindowPresenter):
    """Present a typed notebook surface in the shared standalone viewer."""

    def __init__(self, backend: LiveWindowBackend) -> None:
        super().__init__(
            backend,
            surface_kind="notebook",
            media_type=NOTEBOOK_MEDIA_TYPE,
        )


__all__ = ["JupyterWindowPresenter"]
