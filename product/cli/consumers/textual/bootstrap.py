"""Composition root for the optional Textual host."""
from __future__ import annotations

from typing import Any, Optional

from mote.product.cli.app import build_app
from mote.product.cli.consumers.textual.app import MoteApp
from mote.product.cli.consumers.textual.consumer import TextualConsumer
from mote.product.cli.io.textual_io import TextualPort
from mote.product.cli.surfaces import (
    BrowserWindowPresenter,
    CanvasWindowPresenter,
    DeviceWindowPresenter,
    JupyterWindowPresenter,
    TerminalWindowPresenter,
)
from mote.runtime.interactive.chromium_window import ChromiumLiveWindowBackend
from mote.runtime.interactive.presentation import SurfacePresenterRegistry
from mote.runtime.logging import resume_console_log, suspend_console_log
from mote.runtime.tools.dependency.canvas_backends import DrawioCanvasBackend


def run_textual(
    *,
    model: Optional[str] = None,
    tools: Optional[list] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
    config: Any = None,
) -> None:
    """Assemble and run the full-screen Textual host."""
    app = MoteApp()
    live_window_backend = ChromiumLiveWindowBackend()
    presenters = SurfacePresenterRegistry(
        (
            BrowserWindowPresenter(live_window_backend),
            CanvasWindowPresenter(DrawioCanvasBackend()),
            DeviceWindowPresenter(live_window_backend),
            JupyterWindowPresenter(live_window_backend),
            TerminalWindowPresenter(live_window_backend),
        )
    )
    port = TextualPort(presenters=presenters)
    consumer = TextualConsumer(app)
    driver = build_app(
        model=model,
        tools=tools,
        cwd=cwd,
        name=name,
        consumer_objs=[consumer],
        port=port,
        config=config,
    )
    app.attach(driver, port)
    port.bind_app(app)
    suspended = suspend_console_log()
    try:
        app.run()
    finally:
        if suspended:
            resume_console_log()


__all__ = ["run_textual"]
