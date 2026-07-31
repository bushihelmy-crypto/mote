#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role LSP wiring: opt-in gate, telemetry registration, and cleanup."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from mote.kernel.output import text_output_contract
from mote.product.lsp.factory import ProductLspServiceFactory
from mote.product.paths import default_runtime_paths
from mote.runtime.agent.role import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.wiring import AgentDependencies, AgentWiring
from mote.runtime.config.lsp import LspConfig, LspServerConfig
from mote.runtime.models.clients.context import Context
from mote.ztest.model_fakes import FakeModelGateway, offline_config


class _OfflineLLM:
    model = "test"

    async def aask(self, *_args, **_kwargs):
        return "summary"


def _lsp_config(enabled=True, servers=True):
    srv = [LspServerConfig(name="fake", command=[sys.executable, "-c", ""], extensions=[".py"])] if servers else []
    return LspConfig(enabled=enabled, servers=srv)


def _wiring(context=None):
    root = Path(tempfile.mkdtemp(prefix="mote-lsp-test-"))
    paths = default_runtime_paths(
        user_config_root=root / "config",
        workspace_root=root / "workspace",
    )
    context = context or Context(config=offline_config())
    return AgentWiring.for_context(
        context,
        dependencies=AgentDependencies(
            deps=None,
            output_contract=text_output_contract(),
            lsp_service_factory=ProductLspServiceFactory(),
            user_config_root=paths.user_config_root,
            session_workspace_root=paths.session_workspace_root,
            browser_profiles_root=paths.browser_profiles_root,
            sandbox_ca_root=paths.sandbox_ca_root,
            secrets_root=paths.secrets_root,
            oauth_root=paths.oauth_root,
        ),
    )


def test_lsp_service_none_when_unconfigured():
    r = Role(name="X")
    assert r.role_schema.lsp is None
    assert r.lsp_service is None


def test_lsp_service_none_when_disabled():
    schema = RoleSchema(name="X", lsp=_lsp_config(enabled=False))
    r = Role(role_schema=schema, wiring=_wiring())
    assert r.lsp_service is None


def test_lsp_service_none_when_no_servers():
    schema = RoleSchema(name="X", lsp=_lsp_config(servers=False))
    r = Role(role_schema=schema, wiring=_wiring())
    assert r.lsp_service is None


def test_lsp_service_built_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=_wiring())
    svc = r.lsp_service
    assert svc is not None
    # Cached on the slot.
    assert r.lsp_service is svc
    assert r._components._graph.peek("lsp_service") is svc


def test_slot_starts_none():
    r = Role(name="X")
    assert r._components._graph.peek("lsp_service") is None


def test_lsp_service_registered_with_telemetry_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=_wiring())

    async def scenario():
        await r._components._wire_telemetry()
        handlers = [worker.binding.handler for worker in r.telemetry._workers.values()]
        assert r.lsp_service in handlers
        assert r.lsp_service._telemetry is r.telemetry
        assert r.diagnostics_buffer in handlers
        await r.telemetry.aclose()

    asyncio.run(scenario())


def test_diagnostics_buffer_none_when_unconfigured():
    r = Role(name="X")
    assert r.role_schema.lsp is None
    assert r.diagnostics_buffer is None


def test_diagnostics_buffer_built_and_cached_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=_wiring())
    buf = r.diagnostics_buffer
    assert buf is not None
    assert r.diagnostics_buffer is buf  # cached


def test_turn_context_lsp_source_is_the_buffer():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=_wiring())
    # The buffer is dual-role: the telemetry handler (input) is
    # the turn-context source (output) — no separate wrapper. Same cached
    # instance on both edges, else the staged blocks would never be rendered.
    lsp_source = next(s for s in r.turn_context_bus._sources if s.name == "lsp")
    assert lsp_source is r.diagnostics_buffer
    assert r.diagnostics_buffer in r._components._build_telemetry_subscribers()


def test_no_lsp_handler_when_unconfigured():
    r = Role(name="X", wiring=_wiring())
    assert r.lsp_service is None
    assert not any(hasattr(handler, "drain_diagnostics") for handler in r._components._build_telemetry_subscribers())


def test_cleanup_shuts_down_lsp_service():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=_wiring())
    svc = r.lsp_service

    called = {"n": 0}
    orig = svc.aclose

    async def _spy():
        called["n"] += 1
        await orig()

    svc.aclose = _spy
    asyncio.run(r.cleanup())
    assert called["n"] == 1


def test_cleanup_safe_when_lsp_never_built():
    r = Role(name="X")
    assert r._components._graph.peek("lsp_service") is None
    asyncio.run(r.cleanup())  # no error, no LSP shutdown attempted
    assert r._components._graph.peek("lsp_service") is None
