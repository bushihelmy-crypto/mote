"""Composition root for the optional Textual host."""
from __future__ import annotations

from typing import Any, Optional

from mote.cli.app import build_app
from mote.cli.consumers.textual.app import MoteApp
from mote.cli.consumers.textual.consumer import TextualConsumer
from mote.cli.io.textual_io import TextualPort
from mote.common.logs import resume_console_log, suspend_console_log


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
    port = TextualPort()
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
