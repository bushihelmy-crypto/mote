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
from pathlib import Path
from typing import Any, List, Optional

from mote.orchestration.automation.cron.service import CronService
from mote.product.automation import AgentTriggerAdapter
from mote.product.composition.application import Application
from mote.product.composition.container import ProductContainer
from mote.product.composition.model_reload import ApplicationReloadCoordinator
from mote.product.composition.model_startup import install_initial_application_composition
from mote.product.config.bootstrap import ensure_mote_home
from mote.product.entrypoints.cli import backend
from mote.product.i18n import negotiate_and_set
from mote.product.interaction.commands.catalog import default_registry as default_command_registry
from mote.product.interaction.driver import SessionDriver
from mote.product.interfaces.structured.consumer import build_structured_consumer
from mote.product.interfaces.terminal.consumer import build_terminal_consumer
from mote.product.interfaces.terminal.port import TerminalPort
from mote.product.paths import default_runtime_paths
from mote.product.presentation.projection.base import BaseProjector
from mote.product.presentation.projection.projector import ViewProjector
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource
from mote.runtime.engine import EngineAgentRequest
from mote.runtime.models.failover import LocalModelCallJournal, model_call_journal_root
from mote.runtime.services import EngineServices

_CONSUMER_BUILDERS = {
    "structured": build_structured_consumer,
    "terminal": build_terminal_consumer,
}


async def _install_model_composition(config, container, context, paths):
    composition = await install_initial_application_composition(
        config,
        providers=container.providers,
        oauth_root=paths.oauth_root,
        cost_tracker=context.cost_manager,
        admission_controller=context.model_operator,
        model_call_journal=LocalModelCallJournal(model_call_journal_root(paths.workspace_root)),
    )
    try:
        application_lease = await composition.acquire()
        try:
            runtime_lease = await application_lease.acquire_runtime()
            try:
                backend.configure_service_gateway(
                    context,
                    config,
                    model_profile_gateway=runtime_lease.gateway,
                    media_providers=container.media_providers,
                    search_backends=container.search_backends,
                    paths=paths,
                )
            finally:
                await runtime_lease.aclose()
        finally:
            await application_lease.aclose()
    except BaseException:
        await composition.aclose()
        raise
    return composition


def _build_consumers(config: Any, active: list[str]) -> list[Any]:
    consumers: list[Any] = []
    for name in active:
        builder = _CONSUMER_BUILDERS.get(name)
        if builder is None:
            continue
        try:
            consumers.append(builder(config))
        except Exception:  # noqa: BLE001
            continue
    return consumers


def build_engine(
    *,
    model: Optional[str] = None,
    tools: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
    config: Any = None,
) -> Application:
    """Run first-run scaffolding + build the shared ``config / context / role_factory``.

    The common prefix of the old ``build_app`` body, lifted so a multi-session
    server reuses the exact same construction (no parallel bootstrap path). Side
    effects (``ensure_mote_home`` seeding, locale negotiation) run once here.
    """
    # First-run scaffolding: seed ~/.mote with editable config templates before
    # anything reads it. Idempotent + best-effort — never overwrites, never raises.
    paths = default_runtime_paths()
    ensure_mote_home(
        paths.user_config_root,
        package_dir=paths.package_data_root,
    )

    if config is None:
        config = backend.load_config(model, paths=paths)

    # Resolve the human display locale once, at assembly time, before any consumer
    # renders a line: config.ui.language ("auto" → host LANG/LC_*), then env.
    negotiate_and_set(config_language=getattr(getattr(config, "ui", None), "language", None))

    # Default to the shell's launch directory so every Product catalog is
    # snapshotted against the same workspace the Agent will execute in.
    resolved_cwd = cwd or os.getcwd()
    container = ProductContainer.standard(config, cwd=Path(resolved_cwd), paths=paths)
    context = backend.build_context(
        config,
        providers=container.providers,
        media_providers=container.media_providers,
        search_backends=container.search_backends,
        paths=paths,
    )
    composition = asyncio.run(_install_model_composition(config, container, context, paths))
    services = EngineServices(
        context=context,
        resources=(
            *container.lifecycle_resources(),
            LifecycleResource(
                "application-composition",
                LifecyclePhase.CLOSE_RESOURCES,
                composition.aclose,
            ),
        ),
        application_composition=composition,
        application_reloader=ApplicationReloadCoordinator(
            composition=composition,
            load_config=lambda: backend.load_config(model, paths=paths),
            providers=container.providers,
            oauth_root=paths.oauth_root,
            cost_tracker=context.cost_manager,
            admission_controller=context.model_operator,
            model_call_journal=LocalModelCallJournal(model_call_journal_root(paths.workspace_root)),
        ),
    )

    def role_factory(request):
        """Build a role sharing this app's config + context (initial / new / resume / typed)."""
        return backend.build_role(
            services=services,
            agent_factory=container.agent_factory,
            agent_catalog=container.agents,
            name=request.name,
            tools=tools,
            cwd=resolved_cwd,
            agent_type=request.agent_type,
            session_id=request.session_id,
        )

    return Application(
        container=container,
        services=services,
        agent_factory=role_factory,
    )


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
    paths = eng.container.paths
    role_factory = eng.agent

    role = eng.agent(EngineAgentRequest(name=name))
    control, _ = backend.build_control(role)
    session_id = backend.role_session_id(role)

    # Presentation stack: consumers ← projector ← AgentEvent spine.
    active_consumers = consumer_objs if consumer_objs else _build_consumers(config, consumers or ["terminal"])
    projector = BaseProjector(active_consumers, projector=ViewProjector())
    terminal_port = port if port is not None else TerminalPort()

    # Fire durable ``mote cron add`` tasks into this live session. Tasks with no
    # explicit target default to this session_id (see CronService._on_fire); the
    # driver owns start/stop so the scheduler runs only while the session is up.
    scheduler = CronService(
        AgentTriggerAdapter(control, default_target=session_id),
        session_id=session_id,
        base_dir=str(paths.workspace_root / ".agent_schedules"),
    )

    return SessionDriver(
        control,
        session_id,
        role,
        backend=backend,
        port=terminal_port,
        projector=projector,
        commands=default_command_registry(),
        role_factory=role_factory,
        scheduler=scheduler,
        engine=eng,
        agent_catalog=eng.container.agents,
    )


def run_app(**kwargs) -> None:
    """Build the app and run it to completion (blocking)."""
    driver = build_app(**kwargs)
    asyncio.run(driver.run())


__all__ = ["build_app", "run_app"]
