#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``build_app`` — assemble ``config → control → driver → projector → consumers``.

The §8 successor of ``build_repl``: same ``Config → Context → Role → AgentRuntime
→ AgentControl`` spine, but the presentation side is now the decoupled stack —
a :class:`BaseProjector` fanning the single ``AgentEvent`` fold out to the
configured consumers, an :class:`InteractivePort` (terminal stdin + SIGINT), and
a :class:`SessionDriver` orchestrating turns. Other hosts (web / 飞书 / app-server)
swap only the consumer set + port; the spine is untouched (§4 template).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, List, Optional

from mote.cli import backend
from mote.cli.commands.registry import default_registry
from mote.cli.consumers.registry import build_consumers
from mote.cli.contracts.base import BaseProjector
from mote.cli.driver import SessionDriver
from mote.cli.io.terminal_io import TerminalPort
from mote.cli.view.projector import ViewProjector
from mote.common.config.bootstrap import ensure_mote_home
from mote.common.i18n import negotiate_and_set


def build_app(
    *,
    model: Optional[str] = None,
    tools: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
    consumers: Optional[List[str]] = None,
    consumer_objs: Optional[List[Any]] = None,
    port: Any = None,
    config: Any = None,
) -> SessionDriver:
    """Assemble the terminal app: returns a ready-to-run :class:`SessionDriver`.

    ``consumers`` selects the active consumer channels (default ``["terminal"]``);
    ``port`` overrides the default :class:`TerminalPort` (test seam / alt host).

    ``consumer_objs`` injects **already-built** consumer instances instead of the
    registry (default) — needed by hosts whose consumer cannot be built by name
    because it depends on live host state (e.g. the Textual consumer needs the
    running ``App``). When present it takes precedence over ``consumers``.
    """
    # First-run scaffolding: seed ~/.mote with editable config templates before
    # anything reads it. Idempotent + best-effort — never overwrites, never raises.
    ensure_mote_home()

    if config is None:
        config = backend.load_config(model)

    # Resolve the human display locale once, at assembly time, before any consumer
    # renders a line: config.ui.language ("auto" → host LANG/LC_*), then env. This
    # covers both hosts (Textual routes through build_app too). We deliberately do
    # NOT call locale.setlocale() — only our own process-scoped active locale.
    negotiate_and_set(config_language=getattr(getattr(config, "ui", None), "language", None))

    context = backend.build_context(config)

    # Default to the shell's launch directory so the agent starts where the user
    # invoked the CLI; an explicit --cwd still overrides this.
    cwd = cwd or os.getcwd()

    def role_factory(*, name: str = name, session_id: Optional[str] = None, agent_type: Optional[str] = None):
        """Build a role sharing this app's config + context (initial / new / resume / typed)."""
        return backend.build_role(
            context=context,
            name=name,
            tools=tools,
            cwd=cwd,
            agent_type=agent_type,
            session_id=session_id,
        )

    role = role_factory(name=name)
    control, _ = backend.build_control(role)

    # Presentation stack: consumers ← projector ← AgentEvent spine.
    active_consumers = consumer_objs if consumer_objs else build_consumers(config, active=consumers or ["terminal"])
    projector = BaseProjector(active_consumers, projector=ViewProjector())
    terminal_port = port if port is not None else TerminalPort()

    return SessionDriver(
        control,
        backend.role_session_id(role),
        role,
        port=terminal_port,
        projector=projector,
        commands=default_registry(),
        role_factory=role_factory,
    )


def run_app(**kwargs) -> None:
    """Build the app and run it to completion (blocking)."""
    driver = build_app(**kwargs)
    asyncio.run(driver.run())


__all__ = ["build_app", "run_app"]
