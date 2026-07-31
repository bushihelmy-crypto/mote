"""Composition root for the optional Textual host."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from mote.product.interfaces.textual.app import MoteApp
from mote.product.interfaces.textual.consumer import TextualConsumer
from mote.product.interfaces.textual.port import TextualPort
from mote.product.interfaces.textual.surfaces import (
    CanvasWindowPresenter,
    DeviceWindowPresenter,
    JupyterWindowPresenter,
    TerminalWindowPresenter,
)
from mote.product.interfaces.textual.surfaces.live_window import LiveWindowPresenter
from mote.runtime.interactive.canvas.backends import DrawioCanvasBackend
from mote.runtime.interactive.chromium_window import ChromiumLiveWindowBackend
from mote.runtime.interactive.presentation import SurfacePresenterRegistry
from mote.runtime.telemetry.logging import resume_console_log, suspend_console_log


def run_textual(
    *,
    build_driver: Callable[..., Any],
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
            LiveWindowPresenter(
                live_window_backend,
                surface_kind="browser",
                media_type="application/vnd.mote.browser+json",
            ),
            CanvasWindowPresenter(DrawioCanvasBackend()),
            DeviceWindowPresenter(live_window_backend),
            JupyterWindowPresenter(live_window_backend),
            TerminalWindowPresenter(live_window_backend),
        )
    )
    port = TextualPort(presenters=presenters)
    consumer = TextualConsumer(app)
    driver = build_driver(
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
