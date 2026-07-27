"""Live-surface presenters assembled by CLI hosts."""

from mote.product.cli.surfaces.browser import BrowserWindowPresenter
from mote.product.cli.surfaces.canvas import CanvasWindowPresenter
from mote.product.cli.surfaces.device import DeviceWindowPresenter
from mote.product.cli.surfaces.jupyter import JupyterWindowPresenter
from mote.product.cli.surfaces.terminal import TerminalWindowPresenter

__all__ = [
    "BrowserWindowPresenter",
    "CanvasWindowPresenter",
    "DeviceWindowPresenter",
    "JupyterWindowPresenter",
    "TerminalWindowPresenter",
]
