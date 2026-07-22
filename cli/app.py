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
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from mote.cli import backend
from mote.cli.commands.registry import default_registry
from mote.cli.consumers.registry import build_consumers
from mote.cli.contracts.base import BaseProjector
from mote.cli.driver import SessionDriver
from mote.cli.io.terminal_io import TerminalPort
from mote.cli.view.projector import ViewProjector
from mote.common.config.bootstrap import ensure_mote_home
from mote.common.i18n import negotiate_and_set
from mote.environment.scheduling.service import CronService


@dataclass(frozen=True)
class EngineBuild:
    """The shared engine construction result — everything before ``control``.

    Both the single-session terminal app (:func:`build_app`) and the multi-session
    network server (``cli.serving.SessionRegistry``) construct their sessions from
    this same bundle: one loaded ``config``, one shared engine ``context``, and a
    ``role_factory`` closure that mints roles (new / resume / typed) sharing them.
    Extracting it keeps "how a session is built" in one place so the two hosts can
    never drift (§4 template: hosts swap consumers+port, spine is identical).
    """

    config: Any
    context: Any
    role_factory: Callable[..., Any]


def build_engine(
    *,
    model: Optional[str] = None,
    tools: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
    config: Any = None,
) -> EngineBuild:
    """Run first-run scaffolding + build the shared ``config / context / role_factory``.

    The common prefix of the old ``build_app`` body, lifted so a multi-session
    server reuses the exact same construction (no parallel bootstrap path). Side
    effects (``ensure_mote_home`` seeding, locale negotiation) run once here.
    """
    # First-run scaffolding: seed ~/.mote with editable config templates before
    # anything reads it. Idempotent + best-effort — never overwrites, never raises.
    ensure_mote_home()

    if config is None:
        config = backend.load_config(model)

    # Resolve the human display locale once, at assembly time, before any consumer
    # renders a line: config.ui.language ("auto" → host LANG/LC_*), then env.
    negotiate_and_set(config_language=getattr(getattr(config, "ui", None), "language", None))

    context = backend.build_context(config)

    # Default to the shell's launch directory so the agent starts where the user
    # invoked the CLI; an explicit --cwd still overrides this.
    resolved_cwd = cwd or os.getcwd()

    def role_factory(*, name: str = name, session_id: Optional[str] = None, agent_type: Optional[str] = None):
        """Build a role sharing this app's config + context (initial / new / resume / typed)."""
        return backend.build_role(
            context=context,
            name=name,
            tools=tools,
            cwd=resolved_cwd,
            agent_type=agent_type,
            session_id=session_id,
        )

    return EngineBuild(config=config, context=context, role_factory=role_factory)


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
    # Shared engine construction (config / context / role_factory) — the exact
    # same bundle a multi-session server builds from, so the two hosts can never
    # drift. All side effects (scaffolding, locale) run once inside build_engine.
    eng = build_engine(model=model, tools=tools, cwd=cwd, name=name, config=config)
    config = eng.config
    role_factory = eng.role_factory

    role = role_factory(name=name)
    control, _ = backend.build_control(role)
    session_id = backend.role_session_id(role)

    # Presentation stack: consumers ← projector ← AgentEvent spine.
    active_consumers = consumer_objs if consumer_objs else build_consumers(config, active=consumers or ["terminal"])
    projector = BaseProjector(active_consumers, projector=ViewProjector())
    terminal_port = port if port is not None else TerminalPort()

    # Fire durable ``mote cron add`` tasks into this live session. Tasks with no
    # explicit target default to this session_id (see CronService._on_fire); the
    # driver owns start/stop so the scheduler runs only while the session is up.
    scheduler = CronService(control, session_id=session_id)

    return SessionDriver(
        control,
        session_id,
        role,
        port=terminal_port,
        projector=projector,
        commands=default_registry(),
        role_factory=role_factory,
        scheduler=scheduler,
    )


def run_app(**kwargs) -> None:
    """Build the app and run it to completion (blocking)."""
    driver = build_app(**kwargs)
    asyncio.run(driver.run())


__all__ = ["build_app", "run_app"]
